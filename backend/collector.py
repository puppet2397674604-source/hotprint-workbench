"""Safe Sorftime CategoryProducts adapter with validation, dedup check, dry-run/mock, and confirmation lock."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import base64
import gzip
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import zlib

from .config import (
    API_KEY_PATH,
    CATEGORIES,
    CATEGORY_BY_NODE,
    CONFIGURED_CATEGORIES,
    DEFAULT_COLLECTION_PARAMS,
    DEFAULT_DOMAIN,
    DOMAIN_LABELS,
    PENDING_CATEGORIES,
    SIGNAL_CATEGORIES,
    SIGNAL_CATEGORY_BY_NODE,
    WORKBENCH_API_KEY_PATH,
    beijing_today,
    resolve_category,
    serialize_categories,
    serialize_signal_categories,
)
from .repository import SnapshotRepository

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def _resolve_effective_category(
    identifier: str,
    signals_service: Optional[Any] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve core or signal category (including dynamically configured signal Node IDs)."""
    text = str(identifier or "").strip()
    if not text:
        return None, None
    core_spec = resolve_category(text, include_signals=False)
    if core_spec is not None:
        return core_spec, core_spec.node_id
    if signals_service is not None and hasattr(signals_service, "resolve_effective_signal"):
        sig = signals_service.resolve_effective_signal(text)
        if sig is not None:
            return sig
    sig_spec = resolve_category(text, include_signals=True)
    if sig_spec is not None:
        return sig_spec, sig_spec.node_id
    return None, None


def _parse_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    return None


def build_live_schema_sql() -> str:
    """Build SQLite schema and dynamic 6-category view matching Sorftime snapshot format."""
    when_clauses = "\n".join(
        f"        WHEN '{c.node_id}' THEN '{c.name.replace(chr(39), chr(39) * 2)}'"
        for c in CONFIGURED_CATEGORIES
        if c.node_id
    )
    if not when_clauses:
        when_clauses = "        WHEN '' THEN ''"
    return f"""
CREATE TABLE IF NOT EXISTS category_sales_batches (
    id INTEGER PRIMARY KEY,
    observed_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    domain INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    product_count INTEGER NOT NULL,
    request_consumed INTEGER,
    raw_response TEXT NOT NULL,
    UNIQUE (observed_date, domain, node_id)
);
CREATE TABLE IF NOT EXISTS category_sales_observations (
    batch_id INTEGER NOT NULL REFERENCES category_sales_batches(id),
    result_position INTEGER NOT NULL,
    asin TEXT NOT NULL,
    parent_asin TEXT,
    title TEXT,
    listing_sales_30d INTEGER CHECK (listing_sales_30d >= 0),
    PRIMARY KEY (batch_id, result_position)
);
DROP VIEW IF EXISTS category_sales_by_category;
CREATE VIEW category_sales_by_category AS
WITH observations AS (
    SELECT o.*, b.observed_date, b.domain, b.node_id,
           json_extract(COALESCE(json_extract(b.raw_response, '$.Data.Products'),
                                 json_extract(b.raw_response, '$.Data.products'),
                                 json_extract(b.raw_response, '$.data.Products'),
                                 json_extract(b.raw_response, '$.data.products')),
                        '$[' || (o.result_position - 1) || ']') AS product
    FROM category_sales_observations AS o
    JOIN category_sales_batches AS b ON b.id = o.batch_id
)
SELECT
    o.batch_id,
    o.observed_date,
    o.domain,
    o.node_id,
    CASE o.node_id
{when_clauses}
        ELSE o.node_id
    END AS source_category,
    o.result_position,
    o.asin,
    o.parent_asin,
    CASE
        WHEN o.asin = o.parent_asin THEN '父子编号相同，需核对'
        WHEN o.parent_asin IS NULL THEN '未返回父 ASIN'
        ELSE '父子编号不同'
    END AS asin_status,
    CASE WHEN o.domain = 1 AND o.asin IS NOT o.parent_asin
        THEN 'https://www.amazon.com/dp/' || o.asin
    END AS amazon_url,
    o.title,
    o.listing_sales_30d,
    NULLIF(json_extract(o.product, '$.Ratings'), 0) AS rating_stars,
    json_extract(o.product, '$.RatingsCount') AS ratings_count,
    CASE WHEN o.domain = 1 THEN json_extract(o.product, '$.SalesPrice') / 100.0 END AS sales_price_usd,
    CASE WHEN o.domain = 1 THEN NULLIF(json_extract(o.product, '$.ListingSalesOfMonth'), -1) / 100.0 END
        AS listing_revenue_30d_usd,
    json_extract(o.product, '$.Brand') AS brand,
    json_extract(o.product, '$.Photo[0]') AS main_image_url,
    json_extract(o.product, '$.OnlineDate') AS online_date,
    json_extract(o.product, '$.Rank') AS main_category_bsr,
    (
        SELECT CAST(json_extract(rank.value, '$[2]') AS INTEGER)
        FROM json_each(o.product, '$.BsrCategory') AS rank
        WHERE CAST(json_extract(rank.value, '$[1]') AS TEXT) = o.node_id
        LIMIT 1
    ) AS source_category_bsr
FROM observations AS o;
"""


def bootstrap_live_database(source_db: Path, target_db: Path) -> Path:
    """Create or upgrade an independent writable working SQLite DB without touching source_db."""
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if not target_db.exists() and source_db.is_file() and source_db.resolve() != target_db.resolve():
        tmp_copy = target_db.with_suffix(".tmp_copy")
        shutil.copy2(source_db, tmp_copy)
        tmp_copy.replace(target_db)
    with closing(sqlite3.connect(target_db, timeout=45)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(build_live_schema_sql())
        conn.commit()
    return target_db


def request_sorftime_api(
    endpoint: str,
    params: Dict[str, Any],
    domain: int,
    api_key: str,
    timeout: int = 35,
) -> Dict[str, Any]:
    """Call Sorftime official CLI HTTPS API (stdlib only; never logs api_key)."""
    connection = http.client.HTTPSConnection("cli.sorftime.com", timeout=timeout)
    try:
        connection.request(
            "POST",
            f"/api/{endpoint}?domain={domain}",
            body=json.dumps(params),
            headers={
                "Authorization": f"BasicAuth {api_key}",
                "Content-Type": "application/json",
                "Browser": "Cli",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Sorftime 接口返回 HTTP {response.status}。")
        payload = response.read()
        if response.getheader("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        try:
            result = json.loads(payload)
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                payload = result.encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        try:
            compressed = base64.b64decode(payload.strip(), validate=True)
        except ValueError:
            raise RuntimeError("Sorftime 返回的内容无法解析为 JSON。") from None
        for window_bits in (47, -15):
            try:
                decoded = json.loads(zlib.decompress(compressed, window_bits))
                if isinstance(decoded, dict):
                    return decoded
            except (zlib.error, json.JSONDecodeError):
                continue
        raise RuntimeError("Sorftime 返回的内容无法解析为 JSON。")
    finally:
        connection.close()


class CollectionValidationError(ValueError):
    """Raised when collection parameters fail server-side validation."""


class CollectionConflictError(RuntimeError):
    """Raised on double-submit, expired/replayed token, or concurrent run."""


class CollectionForbiddenError(PermissionError):
    """Raised when live paid API execution is requested while safety lock is enabled."""


class CollectionService:
    """Orchestrates safe manual collection planning, confirmation, dry-run/mock, and audit logs."""

    TOKEN_TTL_SECONDS = 300

    def __init__(
        self,
        repo: SnapshotRepository,
        *,
        allow_live_collection: bool = False,
        api_key_path: Path = API_KEY_PATH,
        workbench_api_key_path: Path = WORKBENCH_API_KEY_PATH,
        live_executor: Optional[Callable[..., Any]] = None,
        signals_service: Optional[Any] = None,
    ):
        self.repo = repo
        self.allow_live_collection = allow_live_collection
        self.api_key_path = api_key_path
        self.workbench_api_key_path = workbench_api_key_path
        self._live_executor = live_executor
        self.signals_service = signals_service
        self._lock = threading.Lock()
        self._run_in_progress = False
        self._secret = secrets.token_bytes(32)
        self._pending_tokens: Dict[str, Dict[str, Any]] = {}
        self._session_runs: List[Dict[str, Any]] = []

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _read_key_file(self, path: Path) -> Optional[str]:
        try:
            if not path.is_file():
                return None
            raw = path.read_text(encoding="utf-8").strip().splitlines()
            if not raw:
                return None
            key = raw[0].strip()
            if key and key.isascii() and key.isprintable():
                return key
            return None
        except (OSError, UnicodeDecodeError):
            return None

    def resolve_api_key(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolve Sorftime API key from local secure paths or env; never expose raw key to API responses."""
        wb_key = self._read_key_file(self.workbench_api_key_path)
        if wb_key:
            return wb_key, "workbench_local"
        legacy_key = self._read_key_file(self.api_key_path)
        if legacy_key:
            return legacy_key, "sorftime_example"
        env_key = (os.environ.get("SORFTIME_API_KEY") or "").strip()
        if env_key and env_key.isascii() and env_key.isprintable():
            return env_key, "env"
        return None, None

    def get_api_key_status(self) -> Dict[str, Any]:
        """Return safe masked status only; NEVER returns full key material."""
        key, source = self.resolve_api_key()
        if not key:
            return {
                "api_key_configured": False,
                "api_key_source": None,
                "api_key_masked": None,
                "api_key_exposed_to_browser": False,
                "can_clear_local_key": self.workbench_api_key_path.is_file(),
            }
        masked = f"****{key[-4:]}" if len(key) >= 8 else "****"
        return {
            "api_key_configured": True,
            "api_key_source": source,
            "api_key_masked": masked,
            "api_key_exposed_to_browser": False,
            "can_clear_local_key": self.workbench_api_key_path.is_file(),
        }

    def save_api_key(self, raw_key: Any) -> Dict[str, Any]:
        """Validate and persist Sorftime API key strictly to gitignored local file with 0600 permissions."""
        key = str(raw_key or "").strip()
        if not key:
            raise CollectionValidationError("Sorftime API 密钥不能为空。")
        if len(key) < 6 or len(key) > 256 or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
            raise CollectionValidationError("API 密钥格式无效（须为 6–256 位不含空白字符的可见 ASCII 字符串）。")
        self.workbench_api_key_path.parent.mkdir(parents=True, exist_ok=True)
        self.workbench_api_key_path.write_text(key + "\n", encoding="utf-8")
        try:
            os.chmod(self.workbench_api_key_path, 0o600)
        except OSError:
            pass
        return {"ok": True, **self.get_api_key_status()}

    def clear_api_key(self) -> Dict[str, Any]:
        """Remove workbench-local saved API key file (does not delete sorftime-example/api_key.local)."""
        try:
            if self.workbench_api_key_path.is_file():
                self.workbench_api_key_path.unlink()
        except OSError as exc:
            raise CollectionValidationError(f"移除本地密钥失败：{exc}") from None
        return {"ok": True, **self.get_api_key_status()}

    def _normalize_categories(self, raw_categories: Any) -> List[str]:
        if raw_categories is None:
            return [c.node_id for c in CONFIGURED_CATEGORIES if c.node_id]
        if not isinstance(raw_categories, list) or not raw_categories:
            raise CollectionValidationError("请至少选择一个已配置 Node ID 的有效目标类目。")
        resolved: List[str] = []
        seen = set()
        for entry in raw_categories:
            text = str(entry).strip()
            spec, eff_node_id = _resolve_effective_category(text, self.signals_service)
            if spec is None:
                raise CollectionValidationError(
                    f"不支持或未核实的类目标识：{text}。严禁使用未经核实的 Sorftime Node ID。"
                )
            if not eff_node_id:
                raise CollectionValidationError(
                    f"类目「{spec.name}（{spec.en_name}）」尚未配置已核实的 Sorftime Node ID（当前状态：待配置），禁止发起采集。"
                )
            if eff_node_id not in seen:
                seen.add(eff_node_id)
                resolved.append(eff_node_id)
        return resolved

    def _validate_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise CollectionValidationError("请求体必须为 JSON 对象。")

        categories = self._normalize_categories(payload.get("categories"))

        try:
            pages = int(payload.get("pages", DEFAULT_COLLECTION_PARAMS["pages"]))
            page_range = int(payload.get("range", DEFAULT_COLLECTION_PARAMS["range"]))
            budget_cap = int(payload.get("budget_cap", DEFAULT_COLLECTION_PARAMS["default_budget_cap"]))
        except (TypeError, ValueError):
            raise CollectionValidationError("页数、每页数量和预算上限必须为整数。") from None

        if not 1 <= pages <= DEFAULT_COLLECTION_PARAMS["max_pages_allowed"]:
            raise CollectionValidationError(
                f"每类采集页数必须在 1 到 {DEFAULT_COLLECTION_PARAMS['max_pages_allowed']} 之间。"
            )
        if not (
            DEFAULT_COLLECTION_PARAMS["min_range_allowed"]
            <= page_range
            <= DEFAULT_COLLECTION_PARAMS["max_range_allowed"]
        ):
            raise CollectionValidationError(
                f"每页数量必须在 {DEFAULT_COLLECTION_PARAMS['min_range_allowed']} 到 "
                f"{DEFAULT_COLLECTION_PARAMS['max_range_allowed']} 之间。"
            )
        if not 1 <= budget_cap <= DEFAULT_COLLECTION_PARAMS["max_budget_cap"]:
            raise CollectionValidationError(
                f"单次预算上限必须在 1 到 {DEFAULT_COLLECTION_PARAMS['max_budget_cap']} 额度之间。"
            )

        mode = str(payload.get("mode", "dry_run")).strip().lower()
        if mode not in ("dry_run", "mock", "live"):
            raise CollectionValidationError("采集执行模式仅支持 dry_run、mock 或 live。")

        observed_date = str(payload.get("observed_date") or beijing_today()).strip()
        if len(observed_date) != 10 or observed_date[4] != "-" or observed_date[7] != "-":
            raise CollectionValidationError("观察日期格式必须为 YYYY-MM-DD（北京时间）。")

        simulate_partial_failure = bool(payload.get("simulate_partial_failure", False))
        return {
            "domain": DEFAULT_DOMAIN,
            "categories": categories,
            "pages": pages,
            "range": page_range,
            "budget_cap": budget_cap,
            "mode": mode,
            "observed_date": observed_date,
            "simulate_partial_failure": simulate_partial_failure,
        }

    def get_config_and_summary(self) -> Dict[str, Any]:
        status, batches = self.repo.get_batches()
        today = beijing_today()
        today_batches = {
            b["node_id"]: b for b in batches if b["observed_date"] == today and b["domain"] == DEFAULT_DOMAIN
        }

        consumed_values = [b["request_consumed"] for b in batches if b["request_consumed"] is not None]
        historical_total_credits = sum(consumed_values)
        latest_batch = batches[0] if batches else None
        per_category_known = (
            round(historical_total_credits / len(consumed_values), 2)
            if consumed_values
            else DEFAULT_COLLECTION_PARAMS["estimated_credits_per_page"]
        )

        category_states = []
        for c in CATEGORIES:
            existing = today_batches.get(c.node_id) if c.node_id else None
            cat_batches = [b for b in batches if c.node_id and b["node_id"] == c.node_id]
            last_cat_batch = cat_batches[0] if cat_batches else None
            category_states.append(
                {
                    "key": c.key,
                    "node_id": c.node_id,
                    "name": c.name,
                    "short_name": c.short_name,
                    "en_name": c.en_name,
                    "garment_type": c.garment_type,
                    "garment_label": c.garment_label,
                    "gender": c.gender,
                    "gender_label": c.gender_label,
                    "segment": c.segment,
                    "is_configured": c.is_configured,
                    "node_status": c.node_status,
                    "status_label": c.status_label,
                    "color": c.color,
                    "soft_color": c.soft_color,
                    "collected_today": existing is not None,
                    "today_product_count": existing["product_count"] if existing else 0,
                    "last_observed_date": last_cat_batch["observed_date"] if last_cat_batch else None,
                    "last_fetched_at": last_cat_batch["fetched_at"] if last_cat_batch else None,
                    "last_page_count": last_cat_batch["page_count"] if last_cat_batch else None,
                    "last_request_consumed": last_cat_batch["request_consumed"] if last_cat_batch else None,
                }
            )

        est_per_page = DEFAULT_COLLECTION_PARAMS["estimated_credits_per_page"]
        key_status = self.get_api_key_status()
        if self.signals_service is not None:
            signal_states = self.signals_service.get_signal_category_states(today=today)
        else:
            signal_states = [
                {
                    **serialize_signal_categories()[idx],
                    "collected_today": False,
                    "today_product_count": 0,
                    "total_asins": 0,
                    "last_observed_date": None,
                    "last_fetched_at": None,
                    "v2_tagged_count": 0,
                    "any_tagged_count": 0,
                }
                for idx in range(len(SIGNAL_CATEGORIES))
            ]
        signal_configured = sum(1 for s in signal_states if s.get("is_configured"))
        return {
            "db_status": status.to_dict(),
            "domain": DEFAULT_DOMAIN,
            "domain_label": DOMAIN_LABELS[DEFAULT_DOMAIN],
            "beijing_today": today,
            "api_endpoint": "CategoryProducts",
            "categories": serialize_categories(),
            "category_states": category_states,
            "category_summary": {
                "total": len(CATEGORIES),
                "configured": len(CONFIGURED_CATEGORIES),
                "pending_config": len(PENDING_CATEGORIES),
                "pending_names": [c.en_name for c in PENDING_CATEGORIES],
            },
            "signal_categories": signal_states,
            "signal_category_states": signal_states,
            "signal_category_summary": {
                "total": len(signal_states),
                "configured": signal_configured,
                "pending_config": len(signal_states) - signal_configured,
                "pending_names": [s["en_name"] for s in signal_states if not s.get("is_configured")],
            },
            "defaults": dict(DEFAULT_COLLECTION_PARAMS),
            "safety": {
                "allow_live_collection": self.allow_live_collection,
                "api_key_configured": key_status["api_key_configured"],
                "api_key_source": key_status["api_key_source"],
                "api_key_masked": key_status["api_key_masked"],
                "can_clear_local_key": key_status["can_clear_local_key"],
                "api_key_exposed_to_browser": False,
                "working_db_name": self.repo.db_path.name,
                "dedup_policy": "按北京时间 observed_date + domain + node_id 每日每类最多写入 1 批成功快照，重复运行自动跳过，失败不自动重试。",
                "unconfigured_node_policy": "尚未核实 Sorftime Node ID 的目标类目标记为「待配置」，前端禁止勾选且后端硬性拦截任何预检或真实请求。",
                "live_script_constraint": "真实采集单次每类固定采集第 1 页（range 10–100，默认 100 条，约 5 额度/类目），新数据写入工作台独立数据库，不修改原始备份库。",
            },
            "quota": {
                "known_per_page_credits": per_category_known,
                "estimated_per_page_credits": est_per_page,
                "configured_single_page_credits": len(CONFIGURED_CATEGORIES) * est_per_page,
                "full_six_category_single_page_credits": len(CATEGORIES) * est_per_page,
                "historical_batches_count": len(batches),
                "historical_total_credits": historical_total_credits,
                "latest_request_left": latest_batch["request_left_after"] if latest_batch else None,
                "latest_observed_date": latest_batch["observed_date"] if latest_batch else None,
                "latest_fetched_at": latest_batch["fetched_at"] if latest_batch else None,
                "estimation_disclaimer": "额度估算参考 Sorftime 文档（约 5 次/页）；下方「实付额度」「响应后余额」均原样读取自 Sorftime 接口 raw_response 中的 RequestConsumed / RequestLeft 字段（未做任何倍乘）。注：2026-11-20 起 Sorftime 接口自身返回的点数为网页端显示次数的 5 倍（如单页返回 25 点 ≈ 网页 5 次、余额 6,500 ≈ 网页 1,300）。",
            },
            "schedule_placeholder": {
                "enabled": False,
                "status": "planned_v0_2",
                "title": "每日定时快照采集（预留入口 · 未启用）",
                "planned_schedule": f"计划每日北京时间 08:30 自动检查并补齐已配置目标类目的当日快照（当前已配置 {len(CONFIGURED_CATEGORIES)}/{len(CATEGORIES)} 类目）",
                "note": "现阶段不运行后台定时任务，所有采集操作均需人工发起与二次确认，避免无人值守扣费。",
            },
            "session_runs": list(reversed(self._session_runs[-10:])),
        }

    def create_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate inputs, inspect same-day dedup skips, estimate quota, and mint single-use token."""
        req = self._validate_request(payload)
        status, batches = self.repo.get_batches()

        existing_map = {
            (b["observed_date"], b["domain"], b["node_id"]): b
            for b in batches
        }

        items: List[Dict[str, Any]] = []
        will_request_count = 0
        for node_id in req["categories"]:
            spec, eff_node_id = _resolve_effective_category(node_id, self.signals_service)
            if spec is None or not eff_node_id:
                raise CollectionValidationError(f"未知的类目节点：{node_id}")
            if spec.category_role == "signal" and self.signals_service is not None:
                sig_cnt = self.signals_service.has_snapshot_for_date(
                    observed_date=req["observed_date"],
                    signal_category_key=spec.key,
                    domain=req["domain"],
                )
                existing = {"id": None, "product_count": sig_cnt} if sig_cnt else None
            else:
                existing = existing_map.get((req["observed_date"], req["domain"], node_id))
            if existing is not None:
                items.append(
                    {
                        "node_id": node_id,
                        "name": spec.name,
                        "category_role": spec.category_role,
                        "action": "skip_existing",
                        "reason": f"{req['observed_date']} 快照已存在（{existing['product_count']} 条），将自动跳过不扣费。",
                        "estimated_credits": 0,
                        "existing_batch_id": existing["id"],
                    }
                )
            else:
                will_request_count += 1
                est = req["pages"] * DEFAULT_COLLECTION_PARAMS["estimated_credits_per_page"]
                items.append(
                    {
                        "node_id": node_id,
                        "name": spec.name,
                        "category_role": spec.category_role,
                        "action": "request",
                        "reason": (
                            f"信号类目当日尚无快照，计划请求 {req['pages']} 页 × {req['range']} 条（写入独立 signal_observations 表）。"
                            if spec.category_role == "signal"
                            else f"当日尚无快照，计划请求 {req['pages']} 页 × {req['range']} 条。"
                        ),
                        "estimated_credits": est,
                        "existing_batch_id": None,
                    }
                )

        estimated_total_credits = sum(i["estimated_credits"] for i in items)
        if estimated_total_credits > req["budget_cap"]:
            raise CollectionValidationError(
                f"预计消耗约 {estimated_total_credits} 额度，超过设定的单次预算上限（{req['budget_cap']} 额度）。请减少类目/页数或调高预算上限。"
            )

        warnings: List[str] = []
        key_status = self.get_api_key_status()
        if req["mode"] == "live" and not self.allow_live_collection:
            warnings.append("当前服务运行在安全隔离模式，禁止真实付费 Sorftime 请求；仅允许 dry_run 或 mock 验证。")
        elif req["mode"] == "live" and not key_status["api_key_configured"] and will_request_count > 0:
            warnings.append("尚未配置本地 Sorftime API 密钥：请先在上方「Sorftime API 密钥配置」中填入密钥后再确认执行。")
        if req["mode"] == "live" and req["pages"] > 1:
            warnings.append(
                "注意：为防止误耗大量额度，真实采集模式当前限定每类每次仅采集第 1 页（pages=1）；请将页数调回 1 后执行。"
            )
        elif req["pages"] > 1 or req["range"] != 100:
            warnings.append(
                "提示：标准首页采集推荐参数为 page=1, range=100（约 5 额度/类目）。"
            )
        if status.state != "ready":
            warnings.append(f"当前本地数据库状态为 {status.state}，同日去重检查基于空批次表进行。")
        if will_request_count == 0:
            warnings.append(f"所选类目在 {req['observed_date']} 均已有成功快照，本次执行预计消耗 0 额度。")

        fingerprint = hashlib.sha256(
            json.dumps(req, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        nonce = secrets.token_hex(12)
        mac = hmac.new(self._secret, f"{nonce}:{fingerprint}".encode("utf-8"), hashlib.sha256).hexdigest()[:20]
        token = f"plan_{nonce}_{mac}"

        now_ts = time.time()
        with self._lock:
            self._prune_tokens(now_ts)
            self._pending_tokens[token] = {
                "fingerprint": fingerprint,
                "request": req,
                "items": items,
                "estimated_total_credits": estimated_total_credits,
                "created_at": now_ts,
                "expires_at": now_ts + self.TOKEN_TTL_SECONDS,
                "used": False,
            }

        return {
            "confirmation_token": token,
            "expires_in_seconds": self.TOKEN_TTL_SECONDS,
            "request": req,
            "items": items,
            "pending_request_categories": will_request_count,
            "skipped_categories": len(items) - will_request_count,
            "estimated_total_credits": estimated_total_credits,
            "actual_charge_guaranteed": False,
            "estimation_note": "预计额度按 5 额度/页参考估算，不承诺固定扣费；dry_run / mock 模式不会产生任何真实扣费。",
            "live_blocked": req["mode"] == "live" and not self.allow_live_collection,
            "warnings": warnings,
        }

    def _prune_tokens(self, now_ts: float) -> None:
        expired = [k for k, v in self._pending_tokens.items() if v["expires_at"] < now_ts or v["used"]]
        for key in expired[:200]:
            self._pending_tokens.pop(key, None)

    def execute_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a confirmed dry_run/mock (or explicitly unlocked live) collection job."""
        if not isinstance(payload, dict):
            raise CollectionValidationError("请求体必须为 JSON 对象。")

        confirmed = payload.get("confirmed")
        token = str(payload.get("confirmation_token") or "").strip()
        if confirmed is not True:
            raise CollectionValidationError("执行采集前必须完成二次确认（confirmed=true）。")
        if not token:
            raise CollectionValidationError("缺少采集计划确认令牌（confirmation_token），请先生成预检计划。")

        now_ts = time.time()
        with self._lock:
            if self._run_in_progress:
                raise CollectionConflictError("已有采集任务正在执行，已阻止重复触发。")

            entry = self._pending_tokens.get(token)
            if entry is None:
                raise CollectionConflictError("确认令牌无效或已过期，请重新执行预检并确认。")
            if entry["used"]:
                raise CollectionConflictError("该确认令牌已被使用，禁止重复提交以防重复扣费。")
            if entry["expires_at"] < now_ts:
                self._pending_tokens.pop(token, None)
                raise CollectionConflictError("确认令牌已超过 5 分钟有效期，请重新预检。")

            req = entry["request"]
            if req["mode"] == "live" and not self.allow_live_collection:
                raise CollectionForbiddenError(
                    "当前工作台处于安全开发模式，已硬性禁止真实 Sorftime 付费请求。请使用 dry_run 或 mock 模式验证采集流程。"
                )

            entry["used"] = True
            self._run_in_progress = True

        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"
        started_at = self._now_iso()
        try:
            plan_items = entry["items"]
            results: List[Dict[str, Any]] = []
            mode = req["mode"]

            for idx, item in enumerate(plan_items):
                node_id = item["node_id"]
                eff_spec, eff_nid = _resolve_effective_category(node_id, self.signals_service)
                if eff_spec is None or not eff_nid or eff_nid != node_id:
                    raise CollectionValidationError(f"禁止采集未核实的类目 Node ID：{node_id}")
                item_role = eff_spec.category_role
                if item["action"] == "skip_existing":
                    results.append(
                        {
                            "node_id": node_id,
                            "name": item["name"],
                            "category_role": item_role,
                            "status": "skipped_existing",
                            "message": item["reason"],
                            "products_returned": 0,
                            "credits_consumed": 0,
                            "retryable": False,
                        }
                    )
                    continue

                if mode == "dry_run":
                    target_note = "（写入独立 signal_observations 表）" if item_role == "signal" else ""
                    results.append(
                        {
                            "node_id": node_id,
                            "name": item["name"],
                            "category_role": item_role,
                            "status": "dry_run_ready",
                            "message": f"[Dry-Run] 校验通过：若开启真实采集将调用 CategoryProducts(nodeId={node_id}, page=1..{req['pages']}, range={req['range']}){target_note}，本次未请求网络、0 扣费。",
                            "products_returned": 0,
                            "credits_consumed": 0,
                            "estimated_credits": item["estimated_credits"],
                            "retryable": False,
                        }
                    )
                elif mode == "mock":
                    if req["simulate_partial_failure"] and idx == len(plan_items) - 1:
                        results.append(
                            {
                                "node_id": node_id,
                                "name": item["name"],
                                "category_role": item_role,
                                "status": "failed",
                                "message": "[Mock 模拟异常] 模拟 Sorftime 上游超时（未扣费），可单独针对该类目重试，不影响其他成功类目。",
                                "products_returned": 0,
                                "credits_consumed": 0,
                                "retryable": True,
                            }
                        )
                    else:
                        results.append(
                            {
                                "node_id": node_id,
                                "name": item["name"],
                                "category_role": item_role,
                                "status": "mock_succeeded",
                                "message": f"[Mock 模拟成功] 模拟返回 {req['pages'] * req['range']} 条观察记录，未写入真实 SQLite，0 实际扣费。",
                                "products_returned": req["pages"] * req["range"],
                                "credits_consumed": 0,
                                "simulated_credits": item["estimated_credits"],
                                "retryable": False,
                            }
                        )
                elif mode == "live":
                    if req["pages"] != 1:
                        raise CollectionValidationError(
                            "为防止误扣高额额度，真实采集模式单次限定每类仅采集第 1 页（pages=1）。"
                        )
                    if self._live_executor is None:
                        raise CollectionForbiddenError("未注入受控真实采集执行器。")
                    try:
                        outcome = self._live_executor(
                            req["observed_date"],
                            node_id,
                            page=1,
                            page_range=req["range"],
                        )
                        if isinstance(outcome, dict):
                            results.append(
                                {
                                    "node_id": node_id,
                                    "name": item["name"],
                                    "category_role": item_role,
                                    "status": outcome.get("status", "succeeded"),
                                    "message": str(outcome.get("message") or "采集成功"),
                                    "products_returned": int(outcome.get("products_returned") or 0),
                                    "credits_consumed": outcome.get("credits_consumed"),
                                    "request_left_after": outcome.get("request_left_after"),
                                    "batch_id": outcome.get("batch_id"),
                                    "retryable": False,
                                }
                            )
                        else:
                            results.append(
                                {
                                    "node_id": node_id,
                                    "name": item["name"],
                                    "category_role": item_role,
                                    "status": "succeeded",
                                    "message": str(outcome),
                                    "products_returned": req["range"],
                                    "credits_consumed": None,
                                    "retryable": False,
                                }
                            )
                    except Exception as exc:
                        results.append(
                            {
                                "node_id": node_id,
                                "name": item["name"],
                                "category_role": item_role,
                                "status": "failed",
                                "message": str(exc),
                                "products_returned": 0,
                                "credits_consumed": None,
                                "retryable": True,
                            }
                        )

            failed_nodes = [r["node_id"] for r in results if r["status"] == "failed"]
            ok_nodes = [
                r["node_id"]
                for r in results
                if r["status"] in ("dry_run_ready", "mock_succeeded", "succeeded", "skipped_existing")
            ]
            succeeded_live_nodes = [r["node_id"] for r in results if r["status"] == "succeeded"]
            core_succeeded = [
                r["node_id"] for r in results if r["status"] == "succeeded" and r.get("category_role") != "signal"
            ]
            signal_succeeded = [
                r["node_id"] for r in results if r["status"] == "succeeded" and r.get("category_role") == "signal"
            ]
            if failed_nodes and ok_nodes:
                overall_status = "partial_success"
            elif failed_nodes:
                overall_status = "failed"
            elif all(r["status"] == "skipped_existing" for r in results):
                overall_status = "skipped_all"
            else:
                overall_status = "completed"

            consumed_vals = [
                int(r["credits_consumed"])
                for r in results
                if r.get("credits_consumed") is not None
            ]
            left_vals = [
                int(r["request_left_after"])
                for r in results
                if r.get("request_left_after") is not None
            ]
            actual_credits = (
                0
                if mode != "live"
                else (sum(consumed_vals) if consumed_vals else (0 if not succeeded_live_nodes else None))
            )

            record = {
                "run_id": run_id,
                "mode": mode,
                "observed_date": req["observed_date"],
                "started_at": started_at,
                "finished_at": self._now_iso(),
                "overall_status": overall_status,
                "real_api_called": mode == "live" and any(item["action"] == "request" for item in plan_items),
                "database_modified": mode == "live" and len(succeeded_live_nodes) > 0,
                "core_database_modified": mode == "live" and len(core_succeeded) > 0,
                "signal_database_modified": mode == "live" and len(signal_succeeded) > 0,
                "retryable_categories": failed_nodes,
                "estimated_credits": entry["estimated_total_credits"],
                "actual_credits_consumed": actual_credits,
                "latest_request_left": left_vals[-1] if left_vals else None,
                "results": results,
            }
            with self._lock:
                self._session_runs.append(record)
                if len(self._session_runs) > 30:
                    self._session_runs = self._session_runs[-30:]
            return record
        finally:
            with self._lock:
                self._run_in_progress = False


def build_sorftime_live_executor(
    db_path: Path,
    api_key_resolver: Callable[[], Tuple[Optional[str], Optional[str]]],
    *,
    api_requester: Callable[..., Dict[str, Any]] = request_sorftime_api,
    signals_service: Optional[Any] = None,
) -> Callable[..., Dict[str, Any]]:
    """Build a standalone live Sorftime CategoryProducts collector.

    - Core apparel categories write to `category_sales_batches` / `category_sales_observations`.
    - Cross-category signal categories write STRICTLY to `signal_observations` (never into core tables).
    """

    def _run_live(
        observed_date: str,
        node_id: str,
        *,
        page: int = 1,
        page_range: int = 100,
    ) -> Dict[str, Any]:
        spec, eff_node_id = _resolve_effective_category(node_id, signals_service)
        if spec is None or not eff_node_id or eff_node_id != node_id:
            raise CollectionValidationError(f"禁止向未核实的类目 Node ID 发起请求：{node_id}")

        api_key, _ = api_key_resolver()
        if not api_key:
            raise RuntimeError("未检测到本地 Sorftime API 密钥，请先在采集页「Sorftime API 密钥配置」中保存密钥。")

        # Signal category path: strictly isolated into `signal_observations` (NEVER enters core 02->04 tables!)
        if spec.category_role == "signal":
            if signals_service is None:
                raise RuntimeError("信号存储服务未初始化，无法写入 signal_observations。")
            existing_cnt = signals_service.has_snapshot_for_date(
                observed_date=observed_date,
                signal_category_key=spec.key,
                domain=DEFAULT_DOMAIN,
            )
            if existing_cnt:
                return {
                    "status": "skipped_existing",
                    "batch_id": None,
                    "products_returned": 0,
                    "credits_consumed": 0,
                    "request_left_after": None,
                    "message": f"信号类目 {spec.name} 在 {observed_date} 已有快照（{existing_cnt} 条），已自动跳过未扣费。",
                }

            response = api_requester(
                "CategoryProducts",
                {"nodeId": node_id, "page": page, "range": page_range},
                DEFAULT_DOMAIN,
                api_key,
            )
            if not isinstance(response, dict):
                raise RuntimeError("Sorftime 返回了非预期的响应结构。")
            code = _parse_int(response.get("Code", response.get("code")))
            consumed = _parse_int(response.get("RequestConsumed", response.get("requestConsumed")))
            left_after = _parse_int(response.get("RequestLeft", response.get("requestLeft")))
            charge_text = str(consumed) if consumed is not None else "未报告"
            if code != 0:
                msg = response.get("Message", response.get("message")) or ""
                raise RuntimeError(f"Sorftime 业务码 {code}（{msg}），额度消耗 {charge_text}。")
            data = response.get("Data", response.get("data"))
            products = data.get("Products", data.get("products")) if isinstance(data, dict) else None
            if not isinstance(products, list):
                raise RuntimeError(f"响应缺少商品列表，额度消耗 {charge_text}。")

            written = signals_service.ingest_signal_snapshot(
                observed_date=observed_date,
                signal_category_key=spec.key,
                node_id=node_id,
                products=products,
                domain=DEFAULT_DOMAIN,
            )
            left_text = f"，接口余额 {left_after}" if left_after is not None else ""
            return {
                "status": "succeeded",
                "batch_id": None,
                "products_returned": written,
                "credits_consumed": consumed,
                "request_left_after": left_after,
                "message": f"信号类目「{spec.name}」写入独立表 signal_observations 共 {written} 条，实付额度 {charge_text}{left_text}。",
            }

        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path, timeout=45)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(build_live_schema_sql())

            # Step 1: check same-day dedup BEFORE making any paid network request
            existing = conn.execute(
                """SELECT id, product_count FROM category_sales_batches
                   WHERE observed_date = ? AND domain = ? AND node_id = ?""",
                (observed_date, DEFAULT_DOMAIN, node_id),
            ).fetchone()
            if existing is not None:
                return {
                    "status": "skipped_existing",
                    "batch_id": int(existing[0]),
                    "products_returned": 0,
                    "credits_consumed": 0,
                    "request_left_after": None,
                    "message": f"{observed_date} 快照已存在（{existing[1]} 条），已自动跳过未扣费。",
                }

            # Step 2: call Sorftime CategoryProducts API
            response = api_requester(
                "CategoryProducts",
                {"nodeId": node_id, "page": page, "range": page_range},
                DEFAULT_DOMAIN,
                api_key,
            )
            if not isinstance(response, dict):
                raise RuntimeError("Sorftime 返回了非预期的响应结构。")

            code = _parse_int(response.get("Code", response.get("code")))
            consumed = _parse_int(response.get("RequestConsumed", response.get("requestConsumed")))
            left_after = _parse_int(response.get("RequestLeft", response.get("requestLeft")))
            charge_text = str(consumed) if consumed is not None else "未报告"
            if code != 0:
                msg = response.get("Message", response.get("message")) or ""
                raise RuntimeError(f"Sorftime 业务码 {code}（{msg}），额度消耗 {charge_text}。")

            data = response.get("Data", response.get("data"))
            products = None
            if isinstance(data, dict):
                products = data.get("Products", data.get("products"))
            if not isinstance(products, list):
                raise RuntimeError(f"响应缺少商品列表，额度消耗 {charge_text}。")

            rows = []
            for position, product in enumerate(products, start=1):
                if not isinstance(product, dict):
                    raise RuntimeError(f"第 {position} 条商品结构无效，额度消耗 {charge_text}。")
                asin = str(product.get("ASIN", product.get("Asin")) or "").strip().upper()
                if not ASIN_PATTERN.fullmatch(asin):
                    raise RuntimeError(f"第 {position} 条商品的 ASIN 无效（{asin}），额度消耗 {charge_text}。")
                parent_asin = str(product.get("ParentAsin", product.get("parentAsin")) or "").strip().upper() or None
                title = str(product.get("Title", product.get("title")) or "").strip() or None
                sales = _parse_int(
                    product.get("ListingSalesVolumeOfMonth", product.get("listingSalesVolumeOfMonth"))
                )
                rows.append((position, asin, parent_asin, title, sales if sales is not None and sales >= 0 else None))

            # Step 3: atomic SQLite commit
            fetched_at = datetime.now(timezone.utc).isoformat()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                existing_again = conn.execute(
                    """SELECT id, product_count FROM category_sales_batches
                       WHERE observed_date = ? AND domain = ? AND node_id = ?""",
                    (observed_date, DEFAULT_DOMAIN, node_id),
                ).fetchone()
                if existing_again is not None:
                    return {
                        "status": "skipped_existing",
                        "batch_id": int(existing_again[0]),
                        "products_returned": 0,
                        "credits_consumed": consumed,
                        "request_left_after": left_after,
                        "message": f"{observed_date} 快照已被并发任务写入（{existing_again[1]} 条）。",
                    }

                cur = conn.execute(
                    """INSERT INTO category_sales_batches
                       (observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observed_date,
                        fetched_at,
                        DEFAULT_DOMAIN,
                        node_id,
                        len(rows),
                        consumed,
                        json.dumps(response, ensure_ascii=False),
                    ),
                )
                batch_id = int(cur.lastrowid)
                conn.executemany(
                    """INSERT INTO category_sales_observations
                       (batch_id, result_position, asin, parent_asin, title, listing_sales_30d)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [(batch_id, pos, asin, parent, title, sales) for pos, asin, parent, title, sales in rows],
                )

            sales_avail = sum(1 for _, _, _, _, s in rows if s is not None)
            left_text = f"，接口余额 {left_after}" if left_after is not None else ""
            return {
                "status": "succeeded",
                "batch_id": batch_id,
                "products_returned": len(rows),
                "sales_available": sales_avail,
                "credits_consumed": consumed,
                "request_left_after": left_after,
                "message": f"真实采集入库 {len(rows)} 条（销量有效 {sales_avail} 条），实付额度 {charge_text}{left_text}。",
            }

    return _run_live
