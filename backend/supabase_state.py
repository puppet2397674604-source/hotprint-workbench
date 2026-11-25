"""Cloud business state synchronization (`select_*` tables) for HotPrint Workbench.

Strict engineering red lines:
- Upstream `pp_*` tables (`pp_records`, `pp_changes`, etc.) are ABSOLUTELY READ-ONLY.
  Any non-`GET` request (`POST`, `PATCH`, `PUT`, `DELETE`) targeting a table that does NOT
  start with `select_` is immediately blocked with `PermissionError` at BOTH the table-helper
  layer (`_request_hp_table`) and the HTTP transport layer (`default_hp_http_requester`).
- Uses Python 3 standard library only (`urllib.request`, `json`, `sqlite3`, `re`).
- Reuses masked credentials from `data/supabase_config.local` (`SupabaseSyncService`) without
  ever exposing plaintext API keys.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .ai_service import AI_CACHE_SCHEMA_SQL
from .config import (
    DEFAULT_SUPABASE_URL,
    WORKBENCH_ROOT,
    WORKBENCH_SUPABASE_CONFIG_PATH,
    beijing_today,
)
from .directions import DIRECTIONS_SCHEMA_SQL
from .preprocessing import APP_SCHEMA_SQL, DEFAULT_APP_DB_PATH
from .scoring import SCORING_SCHEMA_SQL
from .supabase_sync import SupabaseConfigError, SupabaseRequestError

MIGRATION_SQL_PATH = WORKBENCH_ROOT / "backend" / "migrations" / "supabase_select_tables.sql"

SELECT_TABLE_NAME_RE = re.compile(r"^select_[a-z0-9_]+$")
CONTENT_RANGE_TOTAL_RE = re.compile(r"/(\d+)\s*$")

FROZEN_SNAPSHOTS_LOCAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS direction_frozen_snapshots (
    direction_id INTEGER NOT NULL,
    observed_date TEXT NOT NULL,
    direction_name TEXT NOT NULL DEFAULT '',
    direction_score REAL,
    season_tag TEXT NOT NULL DEFAULT 'evergreen',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    frozen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (direction_id, observed_date)
);
"""

SELECT_TABLES_SPEC: Tuple[Dict[str, str], ...] = (
    {
        "hp_table": "select_manual_reviews",
        "local_table": "manual_reviews",
        "label": "人工审核结论",
        "pk_select": "domain,asin",
        "on_conflict": "domain,asin",
        "order": "domain.asc,asin.asc",
    },
    {
        "hp_table": "select_product_ai_cache",
        "local_table": "product_ai_cache",
        "label": "AI 视觉缓存",
        "pk_select": "asin",
        "on_conflict": "asin",
        "order": "asin.asc",
    },
    {
        "hp_table": "select_print_directions",
        "local_table": "print_directions",
        "label": "05 印花方向",
        "pk_select": "id",
        "on_conflict": "id",
        "order": "id.asc",
    },
    {
        "hp_table": "select_direction_asin_links",
        "local_table": "direction_asin_links",
        "label": "方向-ASIN关联",
        "pk_select": "direction_id,asin",
        "on_conflict": "direction_id,asin",
        "order": "direction_id.asc,asin.asc",
    },
    {
        "hp_table": "select_direction_frozen_snapshots",
        "local_table": "direction_frozen_snapshots",
        "label": "06 冻结快照",
        "pk_select": "direction_id,observed_date",
        "on_conflict": "direction_id,observed_date",
        "order": "direction_id.asc,observed_date.asc",
    },
    {
        "hp_table": "select_scoring_presets",
        "local_table": "scoring_presets",
        "label": "04 评分方案",
        "pk_select": "preset_id",
        "on_conflict": "preset_id",
        "order": "preset_id.asc",
    },
)
HP_TABLES_SPEC = SELECT_TABLES_SPEC


def load_select_migration_sql() -> str:
    """Read the idempotent PostgreSQL DDL script for `select_*` tables."""
    if MIGRATION_SQL_PATH.is_file():
        return MIGRATION_SQL_PATH.read_text(encoding="utf-8")
    return ""


load_hp_migration_sql = load_select_migration_sql


def enforce_select_table_write_guard(method: str, table_name: str) -> None:
    """Hard security guard: non-GET methods are strictly forbidden on non-`select_` tables."""
    norm_method = str(method or "GET").strip().upper()
    clean_table = str(table_name or "").strip()
    if not clean_table or "/" in clean_table or "?" in clean_table or ".." in clean_table:
        raise PermissionError(f"安全红线阻断：非法的数据表名 {table_name!r}。")
    if norm_method != "GET":
        if not (clean_table.startswith("select_") and SELECT_TABLE_NAME_RE.fullmatch(clean_table)):
            raise PermissionError(
                f"安全红线阻断：禁止对非 select_ 独立业务表（{clean_table}）发起 {norm_method} 写入请求；上游 pp_* 表绝对只读。"
            )


enforce_hp_table_write_guard = enforce_select_table_write_guard


def _extract_rest_table_from_url(url: str) -> str:
    path = urllib_parse.urlsplit(str(url or "")).path
    marker = "/rest/v1/"
    idx = path.find(marker)
    if idx < 0:
        return ""
    rest = path[idx + len(marker) :].strip("/")
    return rest.split("/", 1)[0] if rest else ""


def default_hp_http_requester(
    method: str,
    url: str,
    headers: Dict[str, str],
    body_bytes: Optional[bytes] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any, Dict[str, str]]:
    """Stdlib HTTP requester for Supabase `select_*` tables with transport-level `pp_*` write block."""
    norm_method = str(method or "GET").strip().upper()
    target_table = _extract_rest_table_from_url(url)
    enforce_select_table_write_guard(norm_method, target_table)

    req = urllib_request.Request(url, data=body_bytes, headers=headers, method=norm_method)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return status, [], resp_headers
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SupabaseRequestError(f"Supabase 返回非 JSON 响应（HTTP {status}）。", status_code=status) from exc
            return status, parsed, resp_headers
    except urllib_error.HTTPError as exc:
        code = int(exc.code)
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:600]
        except Exception:
            err_body = str(exc.reason)
        parsed_err: Any = err_body
        try:
            parsed_err = json.loads(err_body)
        except json.JSONDecodeError:
            pass
        # Return 404 / PGRST205 / 42P01 missing-table responses cleanly so caller can inspect
        if code == 404 or (
            isinstance(parsed_err, dict)
            and str(parsed_err.get("code") or "").upper() in ("PGRST205", "42P01")
        ):
            return code, parsed_err, resp_headers
        msg = (
            parsed_err.get("message") or parsed_err.get("error") or err_body
            if isinstance(parsed_err, dict)
            else (err_body or str(exc.reason))
        )
        raise SupabaseRequestError(f"Supabase 接口返回 HTTP {code}：{msg}", status_code=code) from None
    except urllib_error.URLError as exc:
        raise SupabaseRequestError(f"无法连接 Supabase 服务：{exc.reason}") from exc
    except TimeoutError as exc:
        raise SupabaseRequestError("Supabase 请求超时，请检查网络后重试。", is_timeout=True) from exc
    except OSError as exc:
        raise SupabaseRequestError(f"Supabase 网络请求异常：{exc}") from exc


def _is_missing_table_response(status: int, data: Any) -> bool:
    if status == 404:
        return True
    if isinstance(data, dict):
        err_code = str(data.get("code") or "").strip().upper()
        if err_code in ("PGRST205", "42P01"):
            return True
        msg = str(data.get("message") or data.get("hint") or "").lower()
        if "could not find the table" in msg or ("relation" in msg and "does not exist" in msg):
            return True
    return False


def _parse_json_or_default(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return default


def _to_json_text(raw: Any, default_str: str) -> str:
    if isinstance(raw, (list, dict)):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            return default_str
    return default_str


class SupabaseStateService:
    """Synchronizes local `preprocessing.sqlite3` business state with cloud `select_*` tables."""

    DEFAULT_PUSH_BATCH_SIZE = 200
    DEFAULT_PULL_PAGE_SIZE = 500
    MAX_PULL_PAGES = 200

    def __init__(
        self,
        *,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
        config_path: Path = WORKBENCH_SUPABASE_CONFIG_PATH,
        directions_service: Optional[Any] = None,
        http_requester: Optional[Callable[..., Tuple[int, Any, Dict[str, str]]]] = None,
        timeout: float = 30.0,
    ):
        self.app_db_path = Path(app_db_path)
        self.config_path = Path(config_path)
        self.directions_service = directions_service
        self.http_requester = http_requester or default_hp_http_requester
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._ensure_local_tables()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_local_tables(self) -> None:
        self.app_db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.app_db_path, timeout=20)) as conn:
            conn.executescript(APP_SCHEMA_SQL)
            conn.executescript(AI_CACHE_SCHEMA_SQL)
            conn.executescript(DIRECTIONS_SCHEMA_SQL)
            conn.executescript(SCORING_SCHEMA_SQL)
            conn.executescript(FROZEN_SNAPSHOTS_LOCAL_SCHEMA_SQL)
            # Ensure v2 columns exist on legacy product_ai_cache & print_directions
            ai_cols = {r[1] for r in conn.execute("PRAGMA table_info(product_ai_cache)").fetchall()}
            for col_def in (
                ("prompt_version", "TEXT NOT NULL DEFAULT 'v1'"),
                ("season_tag", "TEXT NOT NULL DEFAULT ''"),
                ("motif", "TEXT NOT NULL DEFAULT ''"),
                ("transferability", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_def[0] not in ai_cols:
                    conn.execute(f"ALTER TABLE product_ai_cache ADD COLUMN {col_def[0]} {col_def[1]}")
            dir_cols = {r[1] for r in conn.execute("PRAGMA table_info(print_directions)").fetchall()}
            for col_def in (
                ("direction_type", "TEXT NOT NULL DEFAULT 'trend_cluster'"),
                ("source", "TEXT NOT NULL DEFAULT 'manual'"),
                ("variant_suggestions", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if col_def[0] not in dir_cols:
                    conn.execute(f"ALTER TABLE print_directions ADD COLUMN {col_def[0]} {col_def[1]}")
            conn.commit()

    def _read_credentials(self) -> Tuple[str, str]:
        url = DEFAULT_SUPABASE_URL
        api_key = ""
        if self.config_path.is_file():
            try:
                parsed = json.loads(self.config_path.read_text(encoding="utf-8").strip() or "{}")
                if isinstance(parsed, dict):
                    if parsed.get("url"):
                        url = str(parsed["url"]).strip().rstrip("/")
                    if parsed.get("api_key"):
                        api_key = str(parsed["api_key"]).strip()
            except Exception:
                pass
        if not api_key:
            api_key = (
                os.environ.get("SUPABASE_API_KEY")
                or os.environ.get("SUPABASE_KEY")
                or os.environ.get("SUPABASE_ANON_KEY")
                or ""
            ).strip()
        if os.environ.get("SUPABASE_URL") and not self.config_path.is_file():
            url = str(os.environ["SUPABASE_URL"]).strip().rstrip("/")
        return url, api_key

    def _require_credentials(self) -> Tuple[str, str]:
        url, api_key = self._read_credentials()
        if not api_key:
            raise SupabaseConfigError("尚未配置 Supabase API Key，请先在 01 页保存配置。")
        return url, api_key

    def _request_hp_table(
        self,
        method: str,
        table_name: str,
        *,
        query_string: str = "",
        payload_obj: Any = None,
        prefer: Optional[str] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        """Execute a guarded REST request against a Supabase `select_*` table."""
        norm_method = str(method or "GET").strip().upper()
        enforce_select_table_write_guard(norm_method, table_name)

        url, api_key = self._require_credentials()
        qs = f"?{query_string.lstrip('?')}" if query_string else ""
        endpoint = f"{url}/rest/v1/{table_name}{qs}"
        headers: Dict[str, str] = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer

        body_bytes: Optional[bytes] = None
        if payload_obj is not None:
            if norm_method == "GET":
                raise ValueError("GET 请求不允许携带请求体。")
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

        return self.http_requester(norm_method, endpoint, headers, body_bytes, self.timeout)

    def _sync_local_shortlisted_frozen_snapshots(self) -> None:
        """Refresh local `direction_frozen_snapshots` from current Stage 06 shortlisted directions."""
        if self.directions_service is None:
            return
        try:
            top10_data = self.directions_service.get_top10()
        except Exception:
            return
        if not isinstance(top10_data, dict) or not top10_data.get("ready"):
            return
        obs_date = str(top10_data.get("selected_date") or beijing_today()).strip()
        items = top10_data.get("items") or []
        if not items:
            return
        now = self._now_iso()
        with closing(sqlite3.connect(self.app_db_path, timeout=20)) as conn:
            conn.executescript(FROZEN_SNAPSHOTS_LOCAL_SCHEMA_SQL)
            for d in items:
                did = d.get("id")
                if did is None:
                    continue
                snap_payload = {
                    "id": int(did),
                    "name": d.get("name") or "",
                    "direction_score": d.get("direction_score"),
                    "season_tag": d.get("season_tag") or "evergreen",
                    "style_tags": d.get("style_tags") or [],
                    "target_audience": d.get("target_audience") or "",
                    "feasibility_rating": d.get("feasibility_rating"),
                    "variant_suggestions": d.get("variant_suggestions") or [],
                    "asin_count": d.get("asin_count"),
                    "parent_asin_count": d.get("parent_asin_count"),
                    "linked_asins": [p.get("asin") for p in (d.get("linked_products") or []) if p.get("asin")],
                }
                conn.execute(
                    """INSERT INTO direction_frozen_snapshots
                       (direction_id, observed_date, direction_name, direction_score, season_tag, snapshot_json, frozen_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(direction_id, observed_date) DO UPDATE SET
                         direction_name = excluded.direction_name,
                         direction_score = excluded.direction_score,
                         season_tag = excluded.season_tag,
                         snapshot_json = excluded.snapshot_json,
                         updated_at = excluded.updated_at""",
                    (
                        int(did),
                        obs_date,
                        str(d.get("name") or ""),
                        float(d["direction_score"]) if d.get("direction_score") is not None else None,
                        str(d.get("season_tag") or "evergreen"),
                        json.dumps(snap_payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            conn.commit()

    def _get_local_table_counts(self) -> Dict[str, int]:
        self._ensure_local_tables()
        self._sync_local_shortlisted_frozen_snapshots()
        counts: Dict[str, int] = {}
        with closing(sqlite3.connect(self.app_db_path, timeout=20)) as conn:
            for spec in SELECT_TABLES_SPEC:
                local_tbl = spec["local_table"]
                row = conn.execute(f"SELECT COUNT(*) FROM {local_tbl}").fetchone()
                counts[spec["hp_table"]] = int(row[0]) if row else 0
        return counts

    def get_hp_status(self) -> Dict[str, Any]:
        """Check whether cloud `select_*` tables exist and compare cloud vs local SQLite row counts."""
        local_counts = self._get_local_table_counts()
        migration_sql = load_select_migration_sql()
        url, api_key = self._read_credentials()

        if not api_key:
            tables_unconfigured = [
                {
                    "table": spec["hp_table"],
                    "local_table": spec["local_table"],
                    "label": spec["label"],
                    "exists": False,
                    "cloud_count": None,
                    "local_count": local_counts.get(spec["hp_table"], 0),
                }
                for spec in SELECT_TABLES_SPEC
            ]
            return {
                "ok": True,
                "configured": False,
                "initialized": False,
                "supabase_url": url,
                "tables": tables_unconfigured,
                "missing_tables": [s["hp_table"] for s in SELECT_TABLES_SPEC],
                "total_cloud_rows": 0,
                "total_local_rows": sum(local_counts.values()),
                "migration_sql": migration_sql,
                "message": "尚未配置 Supabase API Key；可先复制建表 SQL 在云端执行，配置 Key 后即可互通状态。",
            }

        table_statuses: List[Dict[str, Any]] = []
        missing_tables: List[str] = []
        total_cloud = 0

        for spec in SELECT_TABLES_SPEC:
            hp_tbl = spec["hp_table"]
            status, data, resp_headers = self._request_hp_table(
                "GET",
                hp_tbl,
                query_string=f"select={spec['pk_select']}&limit=1",
                prefer="count=exact",
            )
            if _is_missing_table_response(status, data):
                missing_tables.append(hp_tbl)
                table_statuses.append(
                    {
                        "table": hp_tbl,
                        "local_table": spec["local_table"],
                        "label": spec["label"],
                        "exists": False,
                        "cloud_count": None,
                        "local_count": local_counts.get(hp_tbl, 0),
                    }
                )
                continue
            if not (200 <= status < 300):
                raise SupabaseRequestError(f"检测云端表 {hp_tbl} 失败（HTTP {status}）。", status_code=status)

            cloud_cnt = 0
            cr = resp_headers.get("content-range") or ""
            m_tot = CONTENT_RANGE_TOTAL_RE.search(cr)
            if m_tot:
                cloud_cnt = int(m_tot.group(1))
            elif isinstance(data, list):
                cloud_cnt = len(data)
            total_cloud += cloud_cnt
            table_statuses.append(
                {
                    "table": hp_tbl,
                    "local_table": spec["local_table"],
                    "label": spec["label"],
                    "exists": True,
                    "cloud_count": cloud_cnt,
                    "local_count": local_counts.get(hp_tbl, 0),
                }
            )

        initialized = len(missing_tables) == 0
        total_local = sum(local_counts.values())
        return {
            "ok": True,
            "configured": True,
            "initialized": initialized,
            "supabase_url": url,
            "tables": table_statuses,
            "missing_tables": missing_tables,
            "total_cloud_rows": total_cloud,
            "total_local_rows": total_local,
            "migration_sql": migration_sql,
            "message": (
                f"云端业务表已就绪（共 6 张 select_* 表：云端 {total_cloud} 条 / 本地 {total_local} 条）。"
                if initialized
                else f"检测到 {len(missing_tables)} 张云端业务表尚未创建（{', '.join(missing_tables)}），请一键复制建表 SQL 到 Supabase SQL Editor 执行。"
            ),
        }

    def _serialize_local_state_for_push(self) -> Dict[str, List[Dict[str, Any]]]:
        self._ensure_local_tables()
        self._sync_local_shortlisted_frozen_snapshots()
        now = self._now_iso()

        with closing(sqlite3.connect(self.app_db_path, timeout=20)) as conn:
            conn.row_factory = sqlite3.Row

            # 1. select_manual_reviews
            rev_rows = conn.execute("SELECT * FROM manual_reviews ORDER BY domain ASC, asin ASC").fetchall()
            hp_reviews = [
                {
                    "domain": int(r["domain"]),
                    "asin": str(r["asin"]),
                    "decision": str(r["review_status"] or "unreviewed"),
                    "review_status": str(r["review_status"] or "unreviewed"),
                    "reason_code": r["reason_code"],
                    "note": r["reason_note"],
                    "reason_note": r["reason_note"],
                    "operator_source": str(r["operator_source"] or "workbench"),
                    "basis_fingerprint": str(r["basis_fingerprint"] or ""),
                    "last_request_id": r["last_request_id"],
                    "reviewed_at": str(r["reviewed_at"] or now),
                    "updated_at": str(r["reviewed_at"] or now),
                }
                for r in rev_rows
            ]

            # 2. select_product_ai_cache
            ai_rows = conn.execute("SELECT * FROM product_ai_cache ORDER BY asin ASC").fetchall()
            hp_ai_cache = [
                {
                    "asin": str(r["asin"]),
                    "evidence_hash": str(r["evidence_hash"]),
                    "model": str(r["model"] or "deepseek-flash"),
                    "prompt_version": str(r["prompt_version"] or "v2"),
                    "is_graphic_print": bool(r["is_graphic_print"]),
                    "pattern_type": str(r["pattern_type"] or ""),
                    "confidence": float(r["confidence"] or 0.0),
                    "filter_reason": str(r["filter_reason"] or ""),
                    "print_subject": str(r["print_subject"] or ""),
                    "slogan_text": str(r["slogan_text"] or ""),
                    "visual_style": str(r["visual_style"] or ""),
                    "theme_tags": _parse_json_or_default(r["theme_tags_json"], []),
                    "season_tag": str(r["season_tag"] or ""),
                    "motif": str(r["motif"] or ""),
                    "transferability": str(r["transferability"] or ""),
                    "raw_json": _parse_json_or_default(r["raw_json"], {}),
                    "updated_at": str(r["updated_at"] or now),
                }
                for r in ai_rows
            ]

            # 3. select_print_directions
            dir_rows = conn.execute("SELECT * FROM print_directions ORDER BY id ASC").fetchall()
            valid_dir_ids = {int(r["id"]) for r in dir_rows}
            hp_directions = [
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "style_tags": _parse_json_or_default(r["style_tags"], []),
                    "target_audience": str(r["target_audience"] or ""),
                    "feasibility_rating": int(r["feasibility_rating"] or 3),
                    "notes": str(r["notes"] or ""),
                    "status": str(r["status"] or "draft"),
                    "shortlisted": str(r["status"] or "") == "shortlisted",
                    "direction_type": str(r["direction_type"] or "trend_cluster"),
                    "source": str(r["source"] or "manual"),
                    "variant_suggestions": _parse_json_or_default(r["variant_suggestions"], []),
                    "created_at": str(r["created_at"] or now),
                    "updated_at": str(r["updated_at"] or now),
                }
                for r in dir_rows
            ]

            # 4. select_direction_asin_links
            link_rows = conn.execute(
                "SELECT direction_id, asin, added_at FROM direction_asin_links ORDER BY direction_id ASC, asin ASC"
            ).fetchall()
            hp_links = [
                {
                    "direction_id": int(r["direction_id"]),
                    "asin": str(r["asin"]),
                    "added_at": str(r["added_at"] or now),
                }
                for r in link_rows
                if int(r["direction_id"]) in valid_dir_ids
            ]

            # 5. select_direction_frozen_snapshots
            frozen_rows = conn.execute(
                "SELECT * FROM direction_frozen_snapshots ORDER BY direction_id ASC, observed_date ASC"
            ).fetchall()
            hp_frozen = [
                {
                    "direction_id": int(r["direction_id"]),
                    "observed_date": str(r["observed_date"]),
                    "direction_name": str(r["direction_name"] or ""),
                    "direction_score": float(r["direction_score"]) if r["direction_score"] is not None else None,
                    "season_tag": str(r["season_tag"] or "evergreen"),
                    "snapshot_json": _parse_json_or_default(r["snapshot_json"], {}),
                    "frozen_at": str(r["frozen_at"] or now),
                    "updated_at": str(r["updated_at"] or now),
                }
                for r in frozen_rows
                if int(r["direction_id"]) in valid_dir_ids
            ]

            # 6. select_scoring_presets (+ active config flag)
            active_row = conn.execute("SELECT * FROM scoring_active_config WHERE id = 1").fetchone()
            active_pid = str(active_row["preset_id"] or "") if active_row else ""
            preset_rows = conn.execute("SELECT * FROM scoring_presets ORDER BY preset_id ASC").fetchall()
            hp_presets = [
                {
                    "preset_id": str(r["preset_id"]),
                    "name": str(r["name"]),
                    "is_default": bool(r["is_default"]),
                    "is_active": str(r["preset_id"]) == active_pid,
                    "weights_json": _parse_json_or_default(r["weights_json"], {}),
                    "price_ranges_json": _parse_json_or_default(r["price_ranges_json"], {}),
                    "created_at": str(r["created_at"] or now),
                    "updated_at": str(r["updated_at"] or now),
                }
                for r in preset_rows
            ]
            if active_row and active_pid == "custom":
                hp_presets.append(
                    {
                        "preset_id": "__active_custom__",
                        "name": str(active_row["preset_name"] or "自定义权重"),
                        "is_default": False,
                        "is_active": True,
                        "weights_json": _parse_json_or_default(active_row["weights_json"], {}),
                        "price_ranges_json": _parse_json_or_default(active_row["price_ranges_json"], {}),
                        "created_at": str(active_row["updated_at"] or now),
                        "updated_at": str(active_row["updated_at"] or now),
                    }
                )

        return {
            "select_manual_reviews": hp_reviews,
            "select_product_ai_cache": hp_ai_cache,
            "select_print_directions": hp_directions,
            "select_direction_asin_links": hp_links,
            "select_direction_frozen_snapshots": hp_frozen,
            "select_scoring_presets": hp_presets,
        }

    def push_to_cloud(self, *, batch_size: int = DEFAULT_PUSH_BATCH_SIZE) -> Dict[str, Any]:
        """Idempotently upsert local SQLite business state to cloud `select_*` tables in 200-row batches."""
        safe_batch = max(1, min(500, int(batch_size or self.DEFAULT_PUSH_BATCH_SIZE)))
        payload_by_table = self._serialize_local_state_for_push()

        results: List[Dict[str, Any]] = []
        total_pushed = 0

        for spec in SELECT_TABLES_SPEC:
            hp_tbl = spec["hp_table"]
            rows = payload_by_table.get(hp_tbl) or []
            batches_sent = 0
            for start in range(0, len(rows), safe_batch):
                chunk = rows[start : start + safe_batch]
                status, data, _ = self._request_hp_table(
                    "POST",
                    hp_tbl,
                    query_string=f"on_conflict={spec['on_conflict']}",
                    payload_obj=chunk,
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                if _is_missing_table_response(status, data):
                    raise SupabaseRequestError(
                        f"云端表 {hp_tbl} 尚未创建，请先点击「复制建表 SQL」在 Supabase SQL Editor 执行建表。",
                        status_code=404,
                    )
                if not (200 <= status < 300):
                    raise SupabaseRequestError(
                        f"推送 {hp_tbl} 第 {batches_sent + 1} 批失败（HTTP {status}）。",
                        status_code=status,
                    )
                batches_sent += 1
            total_pushed += len(rows)
            results.append(
                {
                    "table": hp_tbl,
                    "label": spec["label"],
                    "pushed_count": len(rows),
                    "batches": batches_sent,
                }
            )

        return {
            "ok": True,
            "action": "push",
            "batch_size": safe_batch,
            "total_pushed": total_pushed,
            "tables": results,
            "message": f"已幂等推送 {total_pushed} 条本地业务状态至云端 select_* 表（每批 {safe_batch} 条）。",
        }

    def _fetch_cloud_hp_table_all(self, spec: Dict[str, str], *, page_size: int) -> List[Dict[str, Any]]:
        hp_tbl = spec["hp_table"]
        rows: List[Dict[str, Any]] = []
        for page_idx in range(self.MAX_PULL_PAGES):
            offset = page_idx * page_size
            qs = f"select=*&order={spec['order']}&limit={page_size}&offset={offset}"
            status, data, _ = self._request_hp_table("GET", hp_tbl, query_string=qs)
            if _is_missing_table_response(status, data):
                raise SupabaseRequestError(
                    f"云端表 {hp_tbl} 尚未创建，请先在 Supabase SQL Editor 执行建表脚本。",
                    status_code=404,
                )
            if not (200 <= status < 300):
                raise SupabaseRequestError(f"拉取云端表 {hp_tbl} 失败（HTTP {status}）。", status_code=status)
            if not isinstance(data, list) or not data:
                break
            valid = [r for r in data if isinstance(r, dict)]
            rows.extend(valid)
            if len(data) < page_size:
                break
        return rows

    def pull_from_cloud(self, *, page_size: int = DEFAULT_PULL_PAGE_SIZE) -> Dict[str, Any]:
        """Paginate cloud `select_*` tables and merge newer/missing state into local `preprocessing.sqlite3`."""
        safe_page = max(1, min(1000, int(page_size or self.DEFAULT_PULL_PAGE_SIZE)))
        self._ensure_local_tables()

        pulled_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for spec in SELECT_TABLES_SPEC:
            pulled_by_table[spec["hp_table"]] = self._fetch_cloud_hp_table_all(spec, page_size=safe_page)

        now = self._now_iso()
        merged_counts: Dict[str, int] = {spec["hp_table"]: 0 for spec in SELECT_TABLES_SPEC}

        with self._lock:
            with closing(sqlite3.connect(self.app_db_path, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                with conn:
                    conn.execute("BEGIN IMMEDIATE")

                    # 1. manual_reviews
                    for r in pulled_by_table["select_manual_reviews"]:
                        asin = str(r.get("asin") or "").strip().upper()
                        if not asin:
                            continue
                        domain = int(r.get("domain") or 1)
                        status_val = str(r.get("review_status") or r.get("decision") or "unreviewed").strip()
                        cloud_ts = str(r.get("updated_at") or r.get("reviewed_at") or now)
                        local_cur = conn.execute(
                            "SELECT reviewed_at FROM manual_reviews WHERE domain = ? AND asin = ?",
                            (domain, asin),
                        ).fetchone()
                        if local_cur is not None and str(local_cur["reviewed_at"] or "") > cloud_ts:
                            continue
                        conn.execute(
                            """INSERT INTO manual_reviews
                               (domain, asin, review_status, reason_code, reason_note, operator_source, reviewed_at, basis_fingerprint, last_request_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(domain, asin) DO UPDATE SET
                                 review_status = excluded.review_status,
                                 reason_code = excluded.reason_code,
                                 reason_note = excluded.reason_note,
                                 operator_source = excluded.operator_source,
                                 reviewed_at = excluded.reviewed_at,
                                 basis_fingerprint = CASE WHEN excluded.basis_fingerprint != '' THEN excluded.basis_fingerprint ELSE manual_reviews.basis_fingerprint END,
                                 last_request_id = excluded.last_request_id""",
                            (
                                domain,
                                asin,
                                status_val,
                                r.get("reason_code"),
                                r.get("reason_note") if r.get("reason_note") is not None else r.get("note"),
                                str(r.get("operator_source") or "supabase_sync"),
                                str(r.get("reviewed_at") or cloud_ts),
                                str(r.get("basis_fingerprint") or ""),
                                r.get("last_request_id"),
                            ),
                        )
                        merged_counts["select_manual_reviews"] += 1

                    # 2. product_ai_cache
                    for r in pulled_by_table["select_product_ai_cache"]:
                        asin = str(r.get("asin") or "").strip().upper()
                        ev_hash = str(r.get("evidence_hash") or "").strip()
                        if not asin or not ev_hash:
                            continue
                        cloud_ts = str(r.get("updated_at") or now)
                        local_cur = conn.execute(
                            "SELECT updated_at FROM product_ai_cache WHERE asin = ?",
                            (asin,),
                        ).fetchone()
                        if local_cur is not None and str(local_cur["updated_at"] or "") > cloud_ts:
                            continue
                        conn.execute(
                            """INSERT INTO product_ai_cache
                               (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type, confidence,
                                filter_reason, print_subject, slogan_text, visual_style, theme_tags_json,
                                season_tag, motif, transferability, raw_json, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(asin) DO UPDATE SET
                                 evidence_hash = excluded.evidence_hash,
                                 model = excluded.model,
                                 prompt_version = excluded.prompt_version,
                                 is_graphic_print = excluded.is_graphic_print,
                                 pattern_type = excluded.pattern_type,
                                 confidence = excluded.confidence,
                                 filter_reason = excluded.filter_reason,
                                 print_subject = excluded.print_subject,
                                 slogan_text = excluded.slogan_text,
                                 visual_style = excluded.visual_style,
                                 theme_tags_json = excluded.theme_tags_json,
                                 season_tag = excluded.season_tag,
                                 motif = excluded.motif,
                                 transferability = excluded.transferability,
                                 raw_json = excluded.raw_json,
                                 updated_at = excluded.updated_at""",
                            (
                                asin,
                                ev_hash,
                                str(r.get("model") or "deepseek-flash"),
                                str(r.get("prompt_version") or "v2"),
                                1 if r.get("is_graphic_print") else 0,
                                str(r.get("pattern_type") or ""),
                                float(r.get("confidence") or 0.0),
                                str(r.get("filter_reason") or ""),
                                str(r.get("print_subject") or ""),
                                str(r.get("slogan_text") or ""),
                                str(r.get("visual_style") or ""),
                                _to_json_text(r.get("theme_tags"), "[]"),
                                str(r.get("season_tag") or ""),
                                str(r.get("motif") or ""),
                                str(r.get("transferability") or ""),
                                _to_json_text(r.get("raw_json"), "{}"),
                                cloud_ts,
                            ),
                        )
                        merged_counts["select_product_ai_cache"] += 1

                    # 3. print_directions
                    for r in pulled_by_table["select_print_directions"]:
                        if r.get("id") is None or not str(r.get("name") or "").strip():
                            continue
                        did = int(r["id"])
                        cloud_ts = str(r.get("updated_at") or now)
                        local_cur = conn.execute(
                            "SELECT updated_at FROM print_directions WHERE id = ?",
                            (did,),
                        ).fetchone()
                        if local_cur is not None and str(local_cur["updated_at"] or "") > cloud_ts:
                            continue
                        status_val = str(r.get("status") or ("shortlisted" if r.get("shortlisted") else "draft"))
                        if status_val not in ("draft", "shortlisted", "archived"):
                            status_val = "draft"
                        feas = max(1, min(5, int(r.get("feasibility_rating") or 3)))
                        conn.execute(
                            """INSERT INTO print_directions
                               (id, name, style_tags, target_audience, feasibility_rating, notes, status,
                                direction_type, source, variant_suggestions, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(id) DO UPDATE SET
                                 name = excluded.name,
                                 style_tags = excluded.style_tags,
                                 target_audience = excluded.target_audience,
                                 feasibility_rating = excluded.feasibility_rating,
                                 notes = excluded.notes,
                                 status = excluded.status,
                                 direction_type = excluded.direction_type,
                                 source = excluded.source,
                                 variant_suggestions = excluded.variant_suggestions,
                                 updated_at = excluded.updated_at""",
                            (
                                did,
                                str(r["name"]).strip(),
                                _to_json_text(r.get("style_tags"), "[]"),
                                str(r.get("target_audience") or ""),
                                feas,
                                str(r.get("notes") or ""),
                                status_val,
                                str(r.get("direction_type") or "trend_cluster"),
                                str(r.get("source") or "manual"),
                                _to_json_text(r.get("variant_suggestions"), "[]"),
                                str(r.get("created_at") or cloud_ts),
                                cloud_ts,
                            ),
                        )
                        merged_counts["select_print_directions"] += 1

                    local_dir_ids = {
                        int(row["id"])
                        for row in conn.execute("SELECT id FROM print_directions").fetchall()
                    }

                    # 4. direction_asin_links
                    for r in pulled_by_table["select_direction_asin_links"]:
                        if r.get("direction_id") is None:
                            continue
                        did = int(r["direction_id"])
                        asin = str(r.get("asin") or "").strip().upper()
                        if did not in local_dir_ids or not asin:
                            continue
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO direction_asin_links
                               (direction_id, asin, added_at)
                               VALUES (?, ?, ?)""",
                            (did, asin, str(r.get("added_at") or now)),
                        )
                        if cur.rowcount > 0:
                            merged_counts["select_direction_asin_links"] += 1

                    # 5. direction_frozen_snapshots
                    for r in pulled_by_table["select_direction_frozen_snapshots"]:
                        if r.get("direction_id") is None or not str(r.get("observed_date") or "").strip():
                            continue
                        did = int(r["direction_id"])
                        obs_d = str(r["observed_date"]).strip()
                        cloud_ts = str(r.get("updated_at") or now)
                        local_cur = conn.execute(
                            "SELECT updated_at FROM direction_frozen_snapshots WHERE direction_id = ? AND observed_date = ?",
                            (did, obs_d),
                        ).fetchone()
                        if local_cur is not None and str(local_cur["updated_at"] or "") > cloud_ts:
                            continue
                        conn.execute(
                            """INSERT INTO direction_frozen_snapshots
                               (direction_id, observed_date, direction_name, direction_score, season_tag, snapshot_json, frozen_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(direction_id, observed_date) DO UPDATE SET
                                 direction_name = excluded.direction_name,
                                 direction_score = excluded.direction_score,
                                 season_tag = excluded.season_tag,
                                 snapshot_json = excluded.snapshot_json,
                                 updated_at = excluded.updated_at""",
                            (
                                did,
                                obs_d,
                                str(r.get("direction_name") or ""),
                                float(r["direction_score"]) if r.get("direction_score") is not None else None,
                                str(r.get("season_tag") or "evergreen"),
                                _to_json_text(r.get("snapshot_json"), "{}"),
                                str(r.get("frozen_at") or cloud_ts),
                                cloud_ts,
                            ),
                        )
                        merged_counts["select_direction_frozen_snapshots"] += 1

                    # 6. scoring_presets (+ active scoring config)
                    for r in pulled_by_table["select_scoring_presets"]:
                        pid = str(r.get("preset_id") or "").strip()
                        if not pid:
                            continue
                        cloud_ts = str(r.get("updated_at") or now)
                        w_txt = _to_json_text(r.get("weights_json"), "{}")
                        p_txt = _to_json_text(r.get("price_ranges_json"), "{}")
                        if pid == "__active_custom__":
                            if r.get("is_active"):
                                act_cur = conn.execute("SELECT updated_at FROM scoring_active_config WHERE id = 1").fetchone()
                                if act_cur is None or str(act_cur["updated_at"] or "") <= cloud_ts:
                                    conn.execute(
                                        """INSERT INTO scoring_active_config
                                           (id, preset_id, preset_name, weights_json, price_ranges_json, updated_at)
                                           VALUES (1, 'custom', ?, ?, ?, ?)
                                           ON CONFLICT(id) DO UPDATE SET
                                             preset_id = 'custom',
                                             preset_name = excluded.preset_name,
                                             weights_json = excluded.weights_json,
                                             price_ranges_json = excluded.price_ranges_json,
                                             updated_at = excluded.updated_at""",
                                        (str(r.get("name") or "自定义权重"), w_txt, p_txt, cloud_ts),
                                    )
                                    merged_counts["select_scoring_presets"] += 1
                            continue

                        local_cur = conn.execute(
                            "SELECT updated_at FROM scoring_presets WHERE preset_id = ?",
                            (pid,),
                        ).fetchone()
                        if local_cur is None or str(local_cur["updated_at"] or "") <= cloud_ts:
                            conn.execute(
                                """INSERT INTO scoring_presets
                                   (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(preset_id) DO UPDATE SET
                                     name = excluded.name,
                                     weights_json = excluded.weights_json,
                                     price_ranges_json = excluded.price_ranges_json,
                                     updated_at = excluded.updated_at""",
                                (
                                    pid,
                                    str(r.get("name") or pid),
                                    1 if (pid == "system_default" or r.get("is_default")) else 0,
                                    w_txt,
                                    p_txt,
                                    str(r.get("created_at") or cloud_ts),
                                    cloud_ts,
                                ),
                            )
                            merged_counts["select_scoring_presets"] += 1

                        if r.get("is_active"):
                            act_cur = conn.execute("SELECT updated_at FROM scoring_active_config WHERE id = 1").fetchone()
                            if act_cur is None or str(act_cur["updated_at"] or "") <= cloud_ts:
                                conn.execute(
                                    """INSERT INTO scoring_active_config
                                       (id, preset_id, preset_name, weights_json, price_ranges_json, updated_at)
                                       VALUES (1, ?, ?, ?, ?, ?)
                                       ON CONFLICT(id) DO UPDATE SET
                                         preset_id = excluded.preset_id,
                                         preset_name = excluded.preset_name,
                                         weights_json = excluded.weights_json,
                                         price_ranges_json = excluded.price_ranges_json,
                                         updated_at = excluded.updated_at""",
                                    (pid, str(r.get("name") or pid), w_txt, p_txt, cloud_ts),
                                )

        total_pulled = sum(len(v) for v in pulled_by_table.values())
        total_merged = sum(merged_counts.values())
        table_summaries = [
            {
                "table": spec["hp_table"],
                "label": spec["label"],
                "pulled_count": len(pulled_by_table[spec["hp_table"]]),
                "merged_count": merged_counts[spec["hp_table"]],
            }
            for spec in SELECT_TABLES_SPEC
        ]
        return {
            "ok": True,
            "action": "pull",
            "total_pulled": total_pulled,
            "total_merged": total_merged,
            "tables": table_summaries,
            "message": f"已从云端 select_* 表拉取 {total_pulled} 条记录，合并更新本地 {total_merged} 条状态。",
        }
