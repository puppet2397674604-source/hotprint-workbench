"""P4 Cross-Category Signal Layer: Independent Storage, Vision v2 Tagging & Relative Heat Comparison.

Strict architectural boundaries:
- Signal observations live ONLY in `signal_observations` (inside `data/preprocessing.sqlite3`) and
  NEVER enter `category_sales_batches`, `category_sales_observations`, Stage 02 preprocessing,
  or Stage 04 candidate scoring pools.
- Signal AI visual analysis lives ONLY in `signal_ai_cache` (identical v2 schema to `product_ai_cache`),
  reusing P2's v2 vision prompt, controlled `motif` vocabulary, standardized `theme_tags`, and
  `dry_run` preview confirmation gate.
- Cross-category heat comparison strictly excludes `form_dependent` items (keeping ONLY
  `pure_graphic` and `text_pun`) and NEVER compares raw absolute sales or mixes BSR across categories;
  every category uses its own within-category relative sales percentile (0..100).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ai_service import (
    AI_SUGGEST_EXCLUDE_CONFIDENCE,
    AIService,
    PATTERN_TYPE_LABELS,
    build_vision_request_payload,
    compute_evidence_hash,
    normalize_theme_tags,
    normalize_vision_result,
    parse_llm_json,
    _extract_assistant_content,
)
from .config import (
    CATEGORY_BY_NODE,
    CategorySpec,
    DEFAULT_DOMAIN,
    MOTIF_LABELS,
    MOTIF_VOCABULARY,
    SEASON_TAG_LABELS,
    SIGNAL_CATEGORIES,
    SIGNAL_CATEGORY_BY_KEY,
    TRANSFERABILITY_LABELS,
    VISION_PROMPT_VERSION,
    beijing_today,
    serialize_signal_categories,
)
from .preprocessing import DEFAULT_APP_DB_PATH, PreprocessingService

NODE_ID_PATTERN = re.compile(r"^[0-9]{4,20}$")


class SignalValidationError(ValueError):
    """Raised when signal category configuration or query parameters are invalid."""


TRANSFERABLE_TYPES = frozenset({"pure_graphic", "text_pun"})

# Gap vs Validated signal classification thresholds (within-category relative percentiles only)
SIGNAL_STRONG_MIN_COUNT = 2
SIGNAL_STRONG_MIN_PERCENTILE = 70.0
CORE_WEAK_MAX_COUNT = 1
CORE_WEAK_MAX_PERCENTILE = 40.0
CORE_STRONG_MIN_COUNT = 2
CORE_STRONG_MIN_PERCENTILE = 50.0

SIGNALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signal_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_date TEXT NOT NULL,
    domain INTEGER NOT NULL DEFAULT 1,
    signal_category_key TEXT NOT NULL,
    node_id TEXT NOT NULL,
    result_position INTEGER NOT NULL,
    asin TEXT NOT NULL,
    parent_asin TEXT,
    title TEXT,
    brand TEXT,
    image_url TEXT,
    listing_sales_30d INTEGER,
    sales_price_usd REAL,
    rating_stars REAL,
    ratings_count INTEGER,
    source_category_bsr INTEGER,
    fetched_at TEXT NOT NULL,
    UNIQUE (observed_date, domain, signal_category_key, asin)
);

CREATE INDEX IF NOT EXISTS idx_signal_obs_cat_date
    ON signal_observations(signal_category_key, observed_date);

CREATE INDEX IF NOT EXISTS idx_signal_obs_asin
    ON signal_observations(asin);

CREATE TABLE IF NOT EXISTS signal_ai_cache (
    asin TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT 'v2',
    is_graphic_print INTEGER NOT NULL,
    pattern_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    filter_reason TEXT NOT NULL DEFAULT '',
    print_subject TEXT NOT NULL DEFAULT '',
    slogan_text TEXT NOT NULL DEFAULT '',
    visual_style TEXT NOT NULL DEFAULT '',
    theme_tags_json TEXT NOT NULL DEFAULT '[]',
    season_tag TEXT NOT NULL DEFAULT '',
    motif TEXT NOT NULL DEFAULT '',
    transferability TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_ai_cache_motif
    ON signal_ai_cache(motif, transferability, is_graphic_print);

CREATE TABLE IF NOT EXISTS signal_category_config (
    signal_category_key TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _within_category_percentiles(rows: List[Tuple[str, float]]) -> Dict[str, float]:
    """Compute within-category relative sales percentile (0..100) for each ASIN.

    Strictly compares items ONLY within the same single category (never across categories).
    Uses midpoint rank percentile `(below + 0.5 * equal) / n * 100` (100.0 when n == 1).
    """
    valid = [(asin, float(sales)) for asin, sales in rows if sales is not None and float(sales) > 0]
    n = len(valid)
    if n == 0:
        return {}
    if n == 1:
        return {valid[0][0]: 100.0}
    vals = [s for _, s in valid]
    out: Dict[str, float] = {}
    for asin, s in valid:
        below = sum(1 for v in vals if v < s)
        equal = sum(1 for v in vals if v == s)
        pct = ((below + 0.5 * equal) / n) * 100.0
        out[asin] = round(max(0.0, min(100.0, pct)), 1)
    return out


def _extract_item_tags(ai_info: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Return list of `(tag_key, tag_type, display_label)` from a transferable v2 AI record."""
    tags: List[Tuple[str, str, str]] = []
    seen = set()

    motif = str(ai_info.get("motif") or "").strip().lower()
    if motif and motif in MOTIF_VOCABULARY and motif != "other":
        k = f"motif:{motif}"
        if k not in seen:
            seen.add(k)
            tags.append((k, "motif", MOTIF_LABELS.get(motif, motif)))

    raw_themes = ai_info.get("theme_tags")
    if isinstance(raw_themes, str):
        try:
            raw_themes = json.loads(raw_themes)
        except json.JSONDecodeError:
            raw_themes = [raw_themes]
    for t in normalize_theme_tags(raw_themes):
        k = f"theme_tag:{t}"
        if k not in seen:
            seen.add(k)
            tags.append((k, "theme_tag", t))

    return tags


def compute_cross_category_signals(
    core_items: List[Dict[str, Any]],
    signal_items_by_category: Dict[str, List[Dict[str, Any]]],
    *,
    signal_strong_min_count: int = SIGNAL_STRONG_MIN_COUNT,
    signal_strong_min_percentile: float = SIGNAL_STRONG_MIN_PERCENTILE,
    core_weak_max_count: int = CORE_WEAK_MAX_COUNT,
    core_weak_max_percentile: float = CORE_WEAK_MAX_PERCENTILE,
    core_strong_min_count: int = CORE_STRONG_MIN_COUNT,
    core_strong_min_percentile: float = CORE_STRONG_MIN_PERCENTILE,
) -> Dict[str, Any]:
    """Pure cross-category heat comparison engine using strictly within-category sales percentiles.

    - Strictly filters to `is_graphic_print == True` AND `transferability in ('pure_graphic', 'text_pun')`.
    - `form_dependent` items are counted in diagnostics and excluded from tag heat aggregation.
    - NEVER compares raw absolute sales or mixes BSR across different categories.
    """
    # 1) Compute within-category sales percentiles for the core apparel pool (strictly per node_id!)
    core_sales_by_node: Dict[str, Dict[str, float]] = {}
    for item in core_items or []:
        asin = str(item.get("asin") or "").strip().upper()
        sales = item.get("listing_sales_30d")
        if not asin or sales is None or float(sales) <= 0:
            continue
        srcs = item.get("source_categories") or []
        if srcs:
            for s in srcs:
                nid = str(s.get("node_id") or s.get("category_key") or "core_default")
                core_sales_by_node.setdefault(nid, {})[asin] = float(sales)
        else:
            core_sales_by_node.setdefault("core_default", {})[asin] = float(sales)

    core_pct_by_node: Dict[str, Dict[str, float]] = {
        nid: _within_category_percentiles(list(asin_map.items()))
        for nid, asin_map in core_sales_by_node.items()
    }
    core_pct_by_asin: Dict[str, float] = {}
    for nid_map in core_pct_by_node.values():
        for asin, pct in nid_map.items():
            if asin not in core_pct_by_asin or pct > core_pct_by_asin[asin]:
                core_pct_by_asin[asin] = pct

    # 2) Compute within-category sales percentiles for each signal category independently
    signal_pct_by_cat: Dict[str, Dict[str, float]] = {}
    for cat_key, items in (signal_items_by_category or {}).items():
        dedup_sales: Dict[str, float] = {}
        for it in items or []:
            asin = str(it.get("asin") or "").strip().upper()
            sales = it.get("listing_sales_30d")
            if asin and sales is not None and float(sales) > 0:
                if asin not in dedup_sales or float(sales) > dedup_sales[asin]:
                    dedup_sales[asin] = float(sales)
        signal_pct_by_cat[cat_key] = _within_category_percentiles(list(dedup_sales.items()))

    # 3) Aggregate transferable tags across core pool & each signal category
    tag_meta: Dict[str, Dict[str, str]] = {}
    core_tag_asins: Dict[str, Dict[str, float]] = {}
    signal_tag_asins: Dict[str, Dict[str, Dict[str, float]]] = {}

    core_transferable_count = 0
    core_filtered_form_dependent = 0
    signal_transferable_count = 0
    signal_filtered_form_dependent = 0

    for item in core_items or []:
        asin = str(item.get("asin") or "").strip().upper()
        ai = item.get("ai_analysis")
        if not asin or not isinstance(ai, dict):
            continue
        if not ai.get("is_graphic_print"):
            continue
        tr = str(ai.get("transferability") or "").strip().lower()
        if tr == "form_dependent":
            core_filtered_form_dependent += 1
            continue
        if tr not in TRANSFERABLE_TYPES:
            continue
        pct = core_pct_by_asin.get(asin)
        if pct is None:
            continue
        core_transferable_count += 1
        for tag_key, tag_type, label in _extract_item_tags(ai):
            tag_meta.setdefault(
                tag_key,
                {
                    "tag_key": tag_key,
                    "tag_code": tag_key.split(":", 1)[1],
                    "tag_type": tag_type,
                    "label": label,
                },
            )
            core_tag_asins.setdefault(tag_key, {})[asin] = pct

    for cat_key, items in (signal_items_by_category or {}).items():
        pct_map = signal_pct_by_cat.get(cat_key, {})
        for it in items or []:
            asin = str(it.get("asin") or "").strip().upper()
            ai = it.get("ai_analysis")
            if not asin or not isinstance(ai, dict):
                continue
            if not ai.get("is_graphic_print"):
                continue
            tr = str(ai.get("transferability") or "").strip().lower()
            if tr == "form_dependent":
                signal_filtered_form_dependent += 1
                continue
            if tr not in TRANSFERABLE_TYPES:
                continue
            pct = pct_map.get(asin)
            if pct is None:
                continue
            signal_transferable_count += 1
            for tag_key, tag_type, label in _extract_item_tags(ai):
                tag_meta.setdefault(
                    tag_key,
                    {
                        "tag_key": tag_key,
                        "tag_code": tag_key.split(":", 1)[1],
                        "tag_type": tag_type,
                        "label": label,
                    },
                )
                signal_tag_asins.setdefault(tag_key, {}).setdefault(cat_key, {})[asin] = pct

    # 4) Build comparison rows & classify Gap vs Validated signals
    rows: List[Dict[str, Any]] = []
    gap_count = 0
    validated_count = 0
    ordered_signal_keys = [c.key for c in SIGNAL_CATEGORIES]
    for extra_k in sorted(signal_items_by_category.keys()):
        if extra_k not in ordered_signal_keys:
            ordered_signal_keys.append(extra_k)

    for tag_key, meta in tag_meta.items():
        core_map = core_tag_asins.get(tag_key, {})
        core_cnt = len(core_map)
        core_avg_pct = (
            round(sum(core_map.values()) / core_cnt, 1) if core_cnt > 0 else None
        )

        per_signal: List[Dict[str, Any]] = []
        strong_signal_cats: List[str] = []
        signal_total_count = 0
        signal_max_avg_pct: Optional[float] = None

        sig_cat_maps = signal_tag_asins.get(tag_key, {})
        for cat_key in ordered_signal_keys:
            amap = sig_cat_maps.get(cat_key, {})
            cnt = len(amap)
            avg_pct = round(sum(amap.values()) / cnt, 1) if cnt > 0 else None
            spec = SIGNAL_CATEGORY_BY_KEY.get(cat_key)
            cat_name = spec.name if spec else cat_key
            is_strong = bool(
                cnt >= signal_strong_min_count
                and avg_pct is not None
                and avg_pct >= signal_strong_min_percentile
            )
            if is_strong:
                strong_signal_cats.append(cat_name)
            if cnt > 0:
                signal_total_count += cnt
                if avg_pct is not None and (signal_max_avg_pct is None or avg_pct > signal_max_avg_pct):
                    signal_max_avg_pct = avg_pct
            per_signal.append(
                {
                    "category_key": cat_key,
                    "name": cat_name,
                    "count": cnt,
                    "avg_percentile": avg_pct,
                    "is_strong": is_strong,
                    "sample_asins": sorted(amap.keys())[:5],
                }
            )

        # Only surface tags that have at least 1 signal observation (cross-category relevance)
        if signal_total_count == 0:
            continue

        signal_is_strong = len(strong_signal_cats) > 0
        core_is_weak = (
            core_cnt <= core_weak_max_count
            or (core_avg_pct is not None and core_avg_pct < core_weak_max_percentile)
        )
        core_is_strong = (
            core_cnt >= core_strong_min_count
            and core_avg_pct is not None
            and core_avg_pct >= core_strong_min_percentile
        )

        if signal_is_strong and core_is_weak:
            signal_type = "gap"
            signal_label = "缺口信号"
            signal_reason = (
                f"在 {'、'.join(strong_signal_cats)} 表现强劲（均分位 ≥ {signal_strong_min_percentile:.0f}），"
                f"但主服饰池覆盖偏弱（{core_cnt} 件"
                + (f"，均分位 {core_avg_pct}" if core_avg_pct is not None else "")
                + "），存在跨品类迁入蓝海机会"
            )
            gap_count += 1
        elif signal_is_strong and core_is_strong:
            signal_type = "validated"
            signal_label = "验证信号"
            signal_reason = (
                f"主服饰池（{core_cnt} 件，均分位 {core_avg_pct}）与 "
                f"{'、'.join(strong_signal_cats)} 同步走强，跨品类交叉验证成立"
            )
            validated_count += 1
        else:
            signal_type = "neutral"
            signal_label = "观察信号"
            signal_reason = "跨品类已有样本，尚未同时达到强缺口或双强验证阈值"

        rows.append(
            {
                **meta,
                "signal_type": signal_type,
                "signal_label": signal_label,
                "signal_reason": signal_reason,
                "core_count": core_cnt,
                "core_avg_percentile": core_avg_pct,
                "core_sample_asins": sorted(core_map.keys())[:5],
                "signal_total_count": signal_total_count,
                "signal_category_count": sum(1 for c in per_signal if c["count"] > 0),
                "signal_max_avg_percentile": signal_max_avg_pct,
                "strong_signal_categories": strong_signal_cats,
                "signal_breakdown": per_signal,
            }
        )

    type_rank = {"gap": 0, "validated": 1, "neutral": 2}
    rows.sort(
        key=lambda r: (
            type_rank.get(r["signal_type"], 9),
            -(r["signal_max_avg_percentile"] if r["signal_max_avg_percentile"] is not None else -1.0),
            -r["signal_total_count"],
            r["tag_key"],
        )
    )

    return {
        "summary": {
            "total_tags": len(rows),
            "gap_count": gap_count,
            "validated_count": validated_count,
            "neutral_count": len(rows) - gap_count - validated_count,
            "core_transferable_count": core_transferable_count,
            "core_filtered_form_dependent": core_filtered_form_dependent,
            "signal_transferable_count": signal_transferable_count,
            "signal_filtered_form_dependent": signal_filtered_form_dependent,
        },
        "thresholds": {
            "signal_strong_min_count": signal_strong_min_count,
            "signal_strong_min_percentile": signal_strong_min_percentile,
            "core_weak_max_count": core_weak_max_count,
            "core_weak_max_percentile": core_weak_max_percentile,
            "core_strong_min_count": core_strong_min_count,
            "core_strong_min_percentile": core_strong_min_percentile,
        },
        "items": rows,
    }


class SignalsService:
    """Manages independent signal_observations, signal_ai_cache, and cross-category heat comparison."""

    def __init__(
        self,
        preprocessing: PreprocessingService,
        ai: AIService,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
    ):
        self.preprocessing = preprocessing
        self.ai = ai
        self.app_db_path = Path(app_db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _connect(self) -> Any:
        self.app_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.app_db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(SIGNALS_SCHEMA_SQL)
                conn.commit()

    # ------------------------------------------------------------------
    # Independent Signal Observation Ingestion & Status
    # ------------------------------------------------------------------
    def has_snapshot_for_date(
        self,
        *,
        observed_date: str,
        signal_category_key: str,
        domain: int = DEFAULT_DOMAIN,
    ) -> Optional[int]:
        """Return observation count if this signal category already has a snapshot on `observed_date`."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS cnt FROM signal_observations
                   WHERE observed_date = ? AND domain = ? AND signal_category_key = ?""",
                (observed_date, int(domain), signal_category_key),
            ).fetchone()
        cnt = int(row["cnt"] or 0) if row else 0
        return cnt if cnt > 0 else None

    def ingest_signal_snapshot(
        self,
        *,
        observed_date: str,
        signal_category_key: str,
        node_id: str,
        products: List[Dict[str, Any]],
        domain: int = DEFAULT_DOMAIN,
        fetched_at: Optional[str] = None,
    ) -> int:
        """Persist signal observations strictly into `signal_observations` (never into core tables)."""
        ts = fetched_at or self._now_iso()
        rows_to_insert = []
        seen_asins = set()
        for pos, p in enumerate(products or [], start=1):
            asin = str(p.get("asin") or p.get("Asin") or p.get("ASIN") or "").strip().upper()
            if not asin or asin in seen_asins:
                continue
            seen_asins.add(asin)
            parent_asin = str(p.get("parent_asin") or p.get("ParentAsin") or "").strip().upper() or None
            title = str(p.get("title") or p.get("Title") or "").strip() or None
            brand = str(p.get("brand") or p.get("Brand") or "").strip() or None
            img = p.get("image_url") or p.get("main_image_url")
            if not img and isinstance(p.get("Photo"), list) and p["Photo"]:
                img = str(p["Photo"][0])
            sales = p.get("listing_sales_30d")
            if sales is None:
                sales = p.get("ListingSalesVolumeOfMonth")
            sales_int = int(sales) if sales is not None and int(sales) >= 0 else None
            price = p.get("sales_price_usd")
            if price is None and p.get("SalesPrice") is not None:
                price = float(p["SalesPrice"]) / 100.0
            rating_stars = p.get("rating_stars") if p.get("rating_stars") is not None else p.get("Ratings")
            if rating_stars == 0:
                rating_stars = None
            ratings_count = p.get("ratings_count") if p.get("ratings_count") is not None else p.get("RatingsCount")
            bsr = p.get("source_category_bsr")
            rows_to_insert.append(
                (
                    observed_date,
                    int(domain),
                    signal_category_key,
                    str(node_id),
                    int(p.get("result_position") or pos),
                    asin,
                    parent_asin,
                    title,
                    brand,
                    str(img or ""),
                    sales_int,
                    float(price) if price is not None else None,
                    float(rating_stars) if rating_stars is not None else None,
                    int(ratings_count) if ratings_count is not None else None,
                    int(bsr) if bsr is not None else None,
                    ts,
                )
            )

        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO signal_observations
                       (observed_date, domain, signal_category_key, node_id, result_position,
                        asin, parent_asin, title, brand, image_url, listing_sales_30d,
                        sales_price_usd, rating_stars, ratings_count, source_category_bsr, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows_to_insert,
                )
                conn.commit()
        return len(rows_to_insert)

    # ------------------------------------------------------------------
    # Dynamic Signal Category Node ID Configuration (Local SQLite)
    # ------------------------------------------------------------------
    def get_configured_node_map(self) -> Dict[str, str]:
        """Return `{signal_category_key: node_id}` merging config defaults and saved local overrides."""
        node_map: Dict[str, str] = {
            spec.key: spec.node_id for spec in SIGNAL_CATEGORIES if spec.node_id
        }
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT signal_category_key, node_id FROM signal_category_config"
            ).fetchall()
        for r in rows:
            k = str(r["signal_category_key"] or "").strip()
            nid = str(r["node_id"] or "").strip()
            if k in SIGNAL_CATEGORY_BY_KEY and nid:
                node_map[k] = nid
        return node_map

    def resolve_effective_signal(self, identifier: str) -> Optional[Tuple[CategorySpec, Optional[str]]]:
        """Resolve a signal category by key or by dynamically configured Node ID."""
        text = str(identifier or "").strip()
        if not text:
            return None
        node_map = self.get_configured_node_map()
        if text in SIGNAL_CATEGORY_BY_KEY:
            spec = SIGNAL_CATEGORY_BY_KEY[text]
            return spec, node_map.get(spec.key)
        for spec in SIGNAL_CATEGORIES:
            eff_nid = node_map.get(spec.key)
            if eff_nid and eff_nid == text:
                return spec, eff_nid
        return None

    def set_signal_node_id(self, signal_category_key: str, raw_node_id: Optional[str]) -> Dict[str, Any]:
        """Save or clear a verified Amazon Browse Node ID for a cross-category signal slot."""
        key = str(signal_category_key or "").strip()
        spec = SIGNAL_CATEGORY_BY_KEY.get(key)
        if spec is None:
            raise SignalValidationError(f"未知的跨品类信号类目：{key}")

        nid = str(raw_node_id or "").strip()
        if not nid:
            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM signal_category_config WHERE signal_category_key = ?",
                        (key,),
                    )
                    conn.commit()
            states = self.get_signal_category_states(today=beijing_today())
            return {
                "ok": True,
                "key": key,
                "node_id": None,
                "message": f"已清除信号类目「{spec.name}」的 Node ID 配置（恢复为待配置 · 服务端阻断）。",
                "signal_category_states": states,
            }

        if not NODE_ID_PATTERN.fullmatch(nid):
            raise SignalValidationError(
                "Amazon Browse Node ID 格式无效：须为 4–20 位纯数字（例如 9302388011）。"
            )
        if nid in CATEGORY_BY_NODE:
            core_spec = CATEGORY_BY_NODE[nid]
            raise SignalValidationError(
                f"Node ID {nid} 已被核心服饰类目「{core_spec.name}」占用，不能重复绑定为信号类目。"
            )
        existing_map = self.get_configured_node_map()
        for other_key, other_nid in existing_map.items():
            if other_key != key and other_nid == nid:
                other_spec = SIGNAL_CATEGORY_BY_KEY.get(other_key)
                other_name = other_spec.name if other_spec else other_key
                raise SignalValidationError(
                    f"Node ID {nid} 已被信号类目「{other_name}」占用，请勿重复绑定。"
                )

        ts = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO signal_category_config
                       (signal_category_key, node_id, updated_at)
                       VALUES (?, ?, ?)""",
                    (key, nid, ts),
                )
                conn.commit()

        states = self.get_signal_category_states(today=beijing_today())
        return {
            "ok": True,
            "key": key,
            "node_id": nid,
            "message": f"已保存信号类目「{spec.name}」的 Node ID（{nid}），现可在手动采集控制台中勾选预检与采集。",
            "signal_category_states": states,
        }

    def get_signal_category_states(self, *, today: str) -> List[Dict[str, Any]]:
        """Return per-signal-category collection & AI tagging state for Stage 01 & Stage 03."""
        node_map = self.get_configured_node_map()
        with self._connect() as conn:
            obs_stats = conn.execute(
                """SELECT signal_category_key,
                          MAX(observed_date) AS latest_date,
                          MAX(fetched_at) AS latest_fetched_at,
                          COUNT(DISTINCT asin) AS total_asins
                   FROM signal_observations
                   WHERE domain = ?
                   GROUP BY signal_category_key""",
                (DEFAULT_DOMAIN,),
            ).fetchall()
            today_stats = conn.execute(
                """SELECT signal_category_key, COUNT(DISTINCT asin) AS cnt
                   FROM signal_observations
                   WHERE domain = ? AND observed_date = ?
                   GROUP BY signal_category_key""",
                (DEFAULT_DOMAIN, today),
            ).fetchall()
            tagged_stats = conn.execute(
                """SELECT o.signal_category_key,
                          COUNT(DISTINCT CASE WHEN c.prompt_version = ? THEN o.asin END) AS v2_cnt,
                          COUNT(DISTINCT CASE WHEN c.asin IS NOT NULL THEN o.asin END) AS any_cnt
                   FROM signal_observations o
                   LEFT JOIN signal_ai_cache c ON c.asin = o.asin
                   WHERE o.domain = ?
                   GROUP BY o.signal_category_key""",
                (VISION_PROMPT_VERSION, DEFAULT_DOMAIN),
            ).fetchall()

        obs_map = {r["signal_category_key"]: dict(r) for r in obs_stats}
        today_map = {r["signal_category_key"]: int(r["cnt"] or 0) for r in today_stats}
        tag_map = {r["signal_category_key"]: dict(r) for r in tagged_stats}

        states: List[Dict[str, Any]] = []
        for spec in SIGNAL_CATEGORIES:
            os_row = obs_map.get(spec.key) or {}
            tg_row = tag_map.get(spec.key) or {}
            today_cnt = today_map.get(spec.key, 0)
            eff_node_id = node_map.get(spec.key)
            eff_configured = bool(eff_node_id)
            states.append(
                {
                    "key": spec.key,
                    "node_id": eff_node_id,
                    "name": spec.name,
                    "short_name": spec.short_name,
                    "en_name": spec.en_name,
                    "category_role": spec.category_role,
                    "role_label": spec.role_label,
                    "garment_type": spec.garment_type,
                    "garment_label": spec.garment_label,
                    "gender": spec.gender,
                    "gender_label": spec.gender_label,
                    "segment": spec.segment,
                    "is_configured": eff_configured,
                    "node_status": "configured" if eff_configured else "pending_config",
                    "status_label": "已配置" if eff_configured else "待配置",
                    "color": spec.color,
                    "soft_color": spec.soft_color,
                    "collected_today": today_cnt > 0,
                    "today_product_count": today_cnt,
                    "total_asins": int(os_row.get("total_asins") or 0),
                    "last_observed_date": os_row.get("latest_date"),
                    "last_fetched_at": os_row.get("latest_fetched_at"),
                    "v2_tagged_count": int(tg_row.get("v2_cnt") or 0),
                    "any_tagged_count": int(tg_row.get("any_cnt") or 0),
                }
            )
        return states

    # ------------------------------------------------------------------
    # Independent `signal_ai_cache` Tagging (Reuses Vision v2 Prompt)
    # ------------------------------------------------------------------
    def _row_to_signal_ai_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        is_print = bool(row["is_graphic_print"])
        conf = float(row["confidence"])
        pattern_type = row["pattern_type"]
        pv = str(row["prompt_version"] or "v1").strip() or "v1"
        season_tag = str(row["season_tag"] or "").strip()
        motif = str(row["motif"] or "").strip()
        transferability = str(row["transferability"] or "").strip()
        return {
            "asin": row["asin"],
            "evidence_hash": row["evidence_hash"],
            "model": row["model"],
            "prompt_version": pv,
            "is_graphic_print": is_print,
            "pattern_type": pattern_type,
            "pattern_label": PATTERN_TYPE_LABELS.get(pattern_type, pattern_type),
            "confidence": conf,
            "filter_reason": row["filter_reason"] or "",
            "print_subject": row["print_subject"] or "",
            "slogan_text": row["slogan_text"] or "",
            "visual_style": row["visual_style"] or "",
            "theme_tags": json.loads(row["theme_tags_json"] or "[]"),
            "season_tag": season_tag,
            "season_label": SEASON_TAG_LABELS.get(season_tag, season_tag) if season_tag else "",
            "motif": motif,
            "motif_label": MOTIF_LABELS.get(motif, motif) if motif else "",
            "transferability": transferability,
            "transferability_label": (
                TRANSFERABILITY_LABELS.get(transferability, transferability)
                if transferability
                else ""
            ),
            "suggest_exclude": (not is_print) and (conf >= AI_SUGGEST_EXCLUDE_CONFIDENCE),
            "updated_at": row["updated_at"],
            "cached": True,
        }

    def _save_signal_ai_cache(
        self,
        *,
        asin: str,
        evidence_hash: str,
        model: str,
        normalized: Dict[str, Any],
        raw_obj: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = self._now_iso()
        pv = str(normalized.get("prompt_version") or VISION_PROMPT_VERSION).strip() or VISION_PROMPT_VERSION
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO signal_ai_cache
                       (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type,
                        confidence, filter_reason, print_subject, slogan_text, visual_style,
                        theme_tags_json, season_tag, motif, transferability, raw_json, updated_at)
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
                        evidence_hash,
                        model,
                        pv,
                        1 if normalized["is_graphic_print"] else 0,
                        normalized["pattern_type"],
                        float(normalized["confidence"]),
                        normalized["filter_reason"],
                        normalized["print_subject"],
                        normalized["slogan_text"],
                        normalized["visual_style"],
                        json.dumps(normalized["theme_tags"], ensure_ascii=False),
                        str(normalized.get("season_tag") or ""),
                        str(normalized.get("motif") or ""),
                        str(normalized.get("transferability") or ""),
                        json.dumps(raw_obj, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()
        return {
            **normalized,
            "asin": asin,
            "evidence_hash": evidence_hash,
            "model": model,
            "prompt_version": pv,
            "updated_at": now,
            "cached": True,
        }

    def _load_latest_signal_observations(
        self, *, category: str = "all"
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Load the latest snapshot observations per signal category, joined with `signal_ai_cache`."""
        with self._connect() as conn:
            if category and category != "all":
                latest_dates = conn.execute(
                    """SELECT signal_category_key, MAX(observed_date) AS latest_date
                       FROM signal_observations
                       WHERE domain = ? AND signal_category_key = ?
                       GROUP BY signal_category_key""",
                    (DEFAULT_DOMAIN, category),
                ).fetchall()
            else:
                latest_dates = conn.execute(
                    """SELECT signal_category_key, MAX(observed_date) AS latest_date
                       FROM signal_observations
                       WHERE domain = ?
                       GROUP BY signal_category_key""",
                    (DEFAULT_DOMAIN,),
                ).fetchall()

            ai_rows = conn.execute("SELECT * FROM signal_ai_cache").fetchall()
            ai_by_asin = {r["asin"]: self._row_to_signal_ai_dict(r) for r in ai_rows}

            result: Dict[str, List[Dict[str, Any]]] = {}
            for ld in latest_dates:
                ckey = ld["signal_category_key"]
                obs_date = ld["latest_date"]
                rows = conn.execute(
                    """SELECT * FROM signal_observations
                       WHERE domain = ? AND signal_category_key = ? AND observed_date = ?
                       ORDER BY result_position ASC, asin ASC""",
                    (DEFAULT_DOMAIN, ckey, obs_date),
                ).fetchall()
                items: List[Dict[str, Any]] = []
                for r in rows:
                    asin = r["asin"]
                    ev_hash = compute_evidence_hash(r["title"], r["image_url"])
                    ai_entry = ai_by_asin.get(asin)
                    valid_ai = (
                        ai_entry
                        if (ai_entry and ai_entry.get("evidence_hash") == ev_hash)
                        else None
                    )
                    items.append(
                        {
                            "asin": asin,
                            "parent_asin": r["parent_asin"],
                            "title": r["title"] or "",
                            "brand": r["brand"] or "",
                            "image_url": r["image_url"] or "",
                            "main_image_url": r["image_url"] or "",
                            "signal_category_key": ckey,
                            "node_id": r["node_id"],
                            "observed_date": r["observed_date"],
                            "result_position": r["result_position"],
                            "listing_sales_30d": r["listing_sales_30d"],
                            "sales_price_usd": r["sales_price_usd"],
                            "rating_stars": r["rating_stars"],
                            "ratings_count": r["ratings_count"],
                            "source_category_bsr": r["source_category_bsr"],
                            "ai_analysis": valid_ai,
                        }
                    )
                result[ckey] = items
        return result

    def _analyze_one_signal_product(self, product: Dict[str, Any], cfg: Dict[str, str]) -> Dict[str, Any]:
        asin = str(product.get("asin") or "").strip().upper()
        image_url = (product.get("main_image_url") or product.get("image_url") or "").strip()
        ev_hash = compute_evidence_hash(product.get("title"), image_url)
        payload = build_vision_request_payload(product, model=cfg["model"])
        resp = self.ai._call_chat_completions(payload, cfg=cfg)
        content = _extract_assistant_content(resp)
        parsed = parse_llm_json(content)
        normalized = normalize_vision_result(parsed, has_image=bool(image_url))
        return self._save_signal_ai_cache(
            asin=asin,
            evidence_hash=ev_hash,
            model=cfg["model"],
            normalized=normalized,
            raw_obj=parsed,
        )

    def tag_signals(
        self,
        *,
        category: str = "all",
        dry_run: bool = False,
        limit: Optional[int] = None,
        max_workers: int = 8,
    ) -> Dict[str, Any]:
        """Run Vision v2 tagging over signal observations into `signal_ai_cache` (with dry_run guard)."""
        cfg_status = self.ai.get_config_status()
        by_cat = self._load_latest_signal_observations(category=category)

        # Deduplicate signal products by ASIN across signal categories
        unique_prods: Dict[str, Dict[str, Any]] = {}
        for items in by_cat.values():
            for it in items:
                if it["asin"] not in unique_prods:
                    unique_prods[it["asin"]] = it

        v2_cached: List[Dict[str, Any]] = []
        v1_upgrade: List[Dict[str, Any]] = []
        brand_new: List[Dict[str, Any]] = []
        for prod in unique_prods.values():
            ai_entry = prod.get("ai_analysis")
            if ai_entry:
                if ai_entry.get("prompt_version") == VISION_PROMPT_VERSION:
                    v2_cached.append(prod)
                else:
                    v1_upgrade.append(prod)
            else:
                brand_new.append(prod)

        pending = brand_new + v1_upgrade
        target = pending[: int(limit)] if (limit is not None and int(limit) > 0) else pending

        if dry_run:
            return {
                "dry_run": True,
                "prompt_version": VISION_PROMPT_VERSION,
                "category": category,
                "total_candidates": len(unique_prods),
                "cached_count": len(v2_cached),
                "new_uncached_count": len(brand_new),
                "upgrade_v1_count": len(v1_upgrade),
                "uncached_count": len(pending),
                "will_analyze_count": len(target),
                "model": cfg_status["model"],
                "base_url": cfg_status["base_url"],
                "has_api_key": cfg_status["has_api_key"],
            }

        analyzed: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        if target:
            cfg = self.ai._require_ready_config()
            workers = max(1, min(int(max_workers or 8), 8, len(target)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self._analyze_one_signal_product, prod, cfg): prod
                    for prod in target
                }
                for fut in as_completed(future_map):
                    prod = future_map[fut]
                    try:
                        analyzed.append(fut.result())
                    except Exception as exc:
                        errors.append({"asin": prod["asin"], "error": str(exc)[:220]})

        return {
            "dry_run": False,
            "prompt_version": VISION_PROMPT_VERSION,
            "category": category,
            "total_candidates": len(unique_prods),
            "cached_skipped": len(v2_cached),
            "analyzed_count": len(analyzed),
            "failed_count": len(errors),
            "errors": errors[:10],
            "model": cfg_status["model"],
        }

    # ------------------------------------------------------------------
    # Stage 03 Cross-Category Signal Comparison API
    # ------------------------------------------------------------------
    def get_overview(
        self,
        *,
        observed_date: Optional[str] = None,
        signal_type: str = "all",
        tag_type: str = "all",
        q: str = "",
    ) -> Dict[str, Any]:
        """Return cross-category signal layer overview & filtered heat comparison table."""
        from .config import beijing_today

        today = beijing_today()
        cat_states = self.get_signal_category_states(today=today)
        signal_by_cat = self._load_latest_signal_observations(category="all")

        # Load core apparel ready pool strictly from Stage 02/04 (read-only)
        core_ready: List[Dict[str, Any]] = []
        selected_date = observed_date
        if self.preprocessing.repo.status().state == "ready":
            selected_date = self.preprocessing._ensure_preprocessed(observed_date)
            all_hydrated = self.preprocessing._hydrate_rows(selected_date)
            core_ready = [
                i
                for i in all_hydrated
                if i["eligibility"] == "ready"
                and i["manual_review_status"] != "confirmed_exclude"
            ]

        comp = compute_cross_category_signals(core_ready, signal_by_cat)
        items = comp["items"]

        st_filter = (signal_type or "all").strip().lower()
        if st_filter in ("gap", "validated", "neutral"):
            items = [i for i in items if i["signal_type"] == st_filter]

        tt_filter = (tag_type or "all").strip().lower()
        if tt_filter in ("motif", "theme_tag"):
            items = [i for i in items if i["tag_type"] == tt_filter]

        query = (q or "").strip().lower()
        if query:
            items = [
                i
                for i in items
                if query in i["label"].lower()
                or query in i["tag_code"].lower()
                or query in i["signal_reason"].lower()
            ]

        total_signal_obs = sum(len(v) for v in signal_by_cat.values())
        return {
            "ready": True,
            "selected_date": selected_date,
            "signal_categories": serialize_signal_categories(),
            "signal_category_states": cat_states,
            "total_signal_observations": total_signal_obs,
            "summary": {
                **comp["summary"],
                "filtered_count": len(items),
            },
            "thresholds": comp["thresholds"],
            "filters": {
                "signal_type": st_filter,
                "tag_type": tt_filter,
                "q": q,
            },
            "items": items,
        }

    def get_signal_products(
        self,
        *,
        category: str = "all",
        ai_status: str = "all",
        q: str = "",
        sort: str = "sales_desc",
        page: int = 1,
        page_size: int = 24,
    ) -> Dict[str, Any]:
        """Return paginated cross-category signal product samples with within-category percentiles."""
        cat_filter = (category or "all").strip()
        by_cat = self._load_latest_signal_observations(category="all")

        # Compute within-category relative sales percentiles independently per signal category
        pct_by_cat: Dict[str, Dict[str, float]] = {}
        category_counts: Dict[str, int] = {}
        for ckey, rows in by_cat.items():
            category_counts[ckey] = len(rows)
            dedup_sales: Dict[str, float] = {}
            for r in rows:
                s = r.get("listing_sales_30d")
                if s is not None and float(s) > 0:
                    dedup_sales[r["asin"]] = float(s)
            pct_by_cat[ckey] = _within_category_percentiles(list(dedup_sales.items()))

        flat: List[Dict[str, Any]] = []
        for ckey, rows in by_cat.items():
            if cat_filter != "all" and ckey != cat_filter:
                continue
            spec = SIGNAL_CATEGORY_BY_KEY.get(ckey)
            pct_map = pct_by_cat.get(ckey, {})
            for r in rows:
                ai = r.get("ai_analysis")
                is_tagged = bool(ai and ai.get("prompt_version") == VISION_PROMPT_VERSION)
                is_transferable = bool(
                    ai
                    and ai.get("is_graphic_print")
                    and str(ai.get("transferability") or "").lower() in TRANSFERABLE_TYPES
                )
                flat.append(
                    {
                        **r,
                        "category_name": spec.name if spec else ckey,
                        "category_short_name": spec.short_name if spec else ckey,
                        "category_color": spec.color if spec else "#D97706",
                        "category_sales_percentile": pct_map.get(r["asin"]),
                        "amazon_url": f"https://www.amazon.com/dp/{r['asin']}",
                        "is_v2_tagged": is_tagged,
                        "is_transferable": is_transferable,
                    }
                )

        ai_mode = (ai_status or "all").strip().lower()
        if ai_mode == "tagged":
            flat = [x for x in flat if x["is_v2_tagged"]]
        elif ai_mode == "untagged":
            flat = [x for x in flat if not x["is_v2_tagged"]]
        elif ai_mode == "transferable":
            flat = [x for x in flat if x["is_transferable"]]

        query = (q or "").strip().lower()
        if query:
            flat = [
                x
                for x in flat
                if query in (x.get("title") or "").lower()
                or query in (x.get("asin") or "").lower()
                or query in (x.get("brand") or "").lower()
                or query in ((x.get("ai_analysis") or {}).get("motif") or "").lower()
            ]

        sort_key = (sort or "sales_desc").strip().lower()
        if sort_key == "percentile_desc":
            flat.sort(
                key=lambda x: (
                    x["category_sales_percentile"] is None,
                    -(x["category_sales_percentile"] or 0.0),
                    -(x.get("listing_sales_30d") or 0),
                )
            )
        elif sort_key == "price_asc":
            flat.sort(
                key=lambda x: (
                    x.get("sales_price_usd") is None or x.get("sales_price_usd") <= 0,
                    x.get("sales_price_usd") or 0.0,
                )
            )
        elif sort_key == "position_asc":
            flat.sort(key=lambda x: (x.get("result_position") or 9999, -(x.get("listing_sales_30d") or 0)))
        else:
            flat.sort(
                key=lambda x: (
                    x.get("listing_sales_30d") is None,
                    -(x.get("listing_sales_30d") or 0),
                    x.get("result_position") or 9999,
                )
            )

        total = len(flat)
        safe_size = max(1, min(60, int(page_size or 24)))
        total_pages = max(1, (total + safe_size - 1) // safe_size)
        safe_page = max(1, min(total_pages, int(page or 1)))
        start = (safe_page - 1) * safe_size
        page_items = flat[start : start + safe_size]

        return {
            "total": total,
            "total_all_categories": sum(category_counts.values()),
            "category_counts": category_counts,
            "page": safe_page,
            "page_size": safe_size,
            "total_pages": total_pages,
            "filters": {
                "category": cat_filter,
                "ai_status": ai_mode,
                "q": q,
                "sort": sort_key,
            },
            "items": page_items,
        }
