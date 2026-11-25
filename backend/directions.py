"""Stage 05: Print Pattern Recognition & Direction Grouping Engine + SQLite Persistence.

Manages:
- `print_directions` and `direction_asin_links` tables in the workbench app SQLite DB
  (`data/preprocessing.sqlite3`), strictly separate from immutable raw Sorftime tables.
- Candidate pool retrieval powered by Stage 04 ScoringService (Top 50 / 80 / 120),
  annotated with each ASIN's `assigned_directions`.
- Direction-level commercial metric aggregation WITHOUT ever summing `listing_sales_30d`:
  - `asin_count` (deduplicated ASINs)
  - `parent_asin_count` (deduplicated independent parent_asin units; missing parent_asin -> own ASIN)
  - `brand_count`, `covered_categories`, `peak_product_score`, `max_sales_30d`, `median_sales_30d`, `price_range`
  - Explainable `direction_score` = peak_product_score + min(20, 10 * ln(parent_asin_count))
    + cross_category_bonus (8 if both T-Shirt & Sweatshirt OR both Women & Men)
    + (feasibility_rating - 3) * 3
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import date, datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ai_service import normalize_variant_suggestions
from .config import (
    HOLIDAY_DATES,
    SEASON_TAGS,
    SEASON_TAG_LABELS,
    beijing_today,
    resolve_category,
    serialize_categories,
)
from .preprocessing import ASIN_RE, DEFAULT_APP_DB_PATH, is_price_anomaly
from .scoring import ScoringService, compute_confidence

DIRECTION_STATUSES = ("draft", "shortlisted", "archived")
DIRECTION_TYPES = ("trend_cluster", "standalone_hit")
DIRECTION_TYPE_LABELS: Dict[str, str] = {
    "trend_cluster": "趋势集群",
    "standalone_hit": "独立爆款",
}
DIRECTION_SOURCES = ("manual", "ai_suggested")
DIRECTION_SOURCE_LABELS: Dict[str, str] = {
    "manual": "人工创建",
    "ai_suggested": "AI 推荐",
}

DIRECTIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS print_directions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    style_tags TEXT NOT NULL DEFAULT '[]',
    target_audience TEXT NOT NULL DEFAULT '',
    feasibility_rating INTEGER NOT NULL DEFAULT 3 CHECK (feasibility_rating BETWEEN 1 AND 5),
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'shortlisted', 'archived')),
    direction_type TEXT NOT NULL DEFAULT 'trend_cluster',
    source TEXT NOT NULL DEFAULT 'manual',
    variant_suggestions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direction_asin_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction_id INTEGER NOT NULL REFERENCES print_directions(id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE(direction_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_direction_links_dir ON direction_asin_links(direction_id);
CREATE INDEX IF NOT EXISTS idx_direction_links_asin ON direction_asin_links(asin);
"""


class DirectionValidationError(ValueError):
    """Raised on invalid direction payload or ASIN link input."""


class DirectionNotFoundError(LookupError):
    """Raised when a print direction ID does not exist."""


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def normalize_style_tags(raw: Any) -> List[str]:
    """Accept JSON list or comma/Chinese-comma separated string and return clean unique tags."""
    if raw is None:
        return []
    tokens: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            if item is not None:
                tokens.append(str(item).strip())
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return normalize_style_tags(parsed)
            except json.JSONDecodeError:
                pass
        for part in re.split(r"[,，、;/|\n]+", stripped):
            if part.strip():
                tokens.append(part.strip())
    else:
        raise DirectionValidationError("style_tags 必须为标签数组或逗号分隔文本。")

    seen = set()
    cleaned: List[str] = []
    for t in tokens:
        t = t[:32]
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            cleaned.append(t)
    return cleaned[:20]


def normalize_asins(raw: Any) -> List[str]:
    """Validate and deduplicate a list of 10-char Amazon ASINs."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DirectionValidationError("asins 必须为数组。")
    seen = set()
    out: List[str] = []
    for item in raw:
        asin = str(item or "").strip().upper()
        if not ASIN_RE.fullmatch(asin):
            raise DirectionValidationError(f"无效的商品 ASIN：{item}。")
        if asin not in seen:
            seen.add(asin)
            out.append(asin)
    return out


def compute_days_until_holiday(
    season_tag: str,
    *,
    reference_date: Optional[date] = None,
    holiday_dates: Optional[Dict[str, str]] = None,
) -> Optional[int]:
    """Return signed day count until the target holiday (None for evergreen or unmapped seasonal)."""
    tag = str(season_tag or "").strip().lower()
    if not tag or tag == "evergreen":
        return None
    cal = holiday_dates if holiday_dates is not None else HOLIDAY_DATES
    raw_target = cal.get(tag)
    if not raw_target:
        return None
    try:
        target_dt = date.fromisoformat(str(raw_target)[:10])
    except ValueError:
        return None
    ref_dt = reference_date if isinstance(reference_date, date) else date.fromisoformat(beijing_today())
    return (target_dt - ref_dt).days


def compute_direction_season(
    linked_products: List[Dict[str, Any]],
    *,
    reference_date: Optional[date] = None,
    holiday_dates: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Derive direction season by majority vote over linked products' cached `season_tag`.

    - Uses `product.ai_analysis.season_tag` (or direct `product.season_tag` if present).
    - Falls back cleanly to `evergreen` when there is no cached season data, when `evergreen`
      wins or ties for the top count, or when multiple seasonal tags tie for the lead.
    - Computes `days_until_holiday` purely as informational countdown (never hard-blocks).
    """
    vote_counts: Dict[str, int] = {}
    for p in linked_products or []:
        ai = p.get("ai_analysis") if isinstance(p.get("ai_analysis"), dict) else {}
        raw_tag = ai.get("season_tag") or p.get("season_tag") or ""
        tag = str(raw_tag).strip().lower()
        if tag in SEASON_TAGS:
            vote_counts[tag] = vote_counts.get(tag, 0) + 1

    if not vote_counts:
        winner = "evergreen"
    else:
        max_votes = max(vote_counts.values())
        top_tags = [k for k, v in vote_counts.items() if v == max_votes]
        if "evergreen" in top_tags or len(top_tags) > 1:
            winner = "evergreen"
        else:
            winner = top_tags[0]

    cal = holiday_dates if holiday_dates is not None else HOLIDAY_DATES
    days_until = compute_days_until_holiday(
        winner, reference_date=reference_date, holiday_dates=cal
    )
    return {
        "season_tag": winner,
        "season_label": SEASON_TAG_LABELS.get(winner, winner),
        "is_seasonal": winner != "evergreen",
        "season_bucket": "seasonal" if winner != "evergreen" else "evergreen",
        "holiday_date": cal.get(winner) if winner != "evergreen" else None,
        "days_until_holiday": days_until,
        "season_votes": vote_counts,
    }


def aggregate_direction_metrics(
    linked_products: List[Dict[str, Any]],
    *,
    feasibility_rating: int = 3,
) -> Dict[str, Any]:
    """Pure direction-level commercial aggregation (never sums listing_sales_30d).

    Deduplicates linked products defensively by ASIN, folds independent market breadth
    by `parent_asin` (falling back to own `asin` when parent_asin is missing), and
    returns fully explainable `direction_score` breakdown.
    """
    feas = max(1, min(5, int(feasibility_rating or 3)))

    # Defensive ASIN deduplication
    unique_by_asin: Dict[str, Dict[str, Any]] = {}
    for p in linked_products or []:
        asin = str(p.get("asin") or "").strip().upper()
        if asin and asin not in unique_by_asin:
            unique_by_asin[asin] = p
    items = list(unique_by_asin.values())

    asin_count = len(items)
    if asin_count == 0:
        return {
            "asin_count": 0,
            "parent_asin_count": 0,
            "brand_count": 0,
            "covered_categories": [],
            "garment_types": [],
            "genders": [],
            "peak_product_score": None,
            "raw_peak_product_score": None,
            "all_anomalous": False,
            "excluded_anomaly_count": 0,
            "anomalous_asins": [],
            "anomalous_items": [],
            "max_sales_30d": None,
            "median_sales_30d": None,
            "price_range": None,
            "direction_score": 0.0,
            "score_breakdown": {
                "base_score": 0.0,
                "raw_peak_score": None,
                "all_anomalous": False,
                "excluded_anomaly_count": 0,
                "anomalous_asins": [],
                "breadth_bonus": 0.0,
                "cross_category_bonus": 0.0,
                "cross_category_reason": "暂无关联商品",
                "feasibility_delta": round((feas - 3) * 3.0, 1),
                "total": 0.0,
            },
        }

    # Independent parent_asin count (missing/blank parent_asin counts as own ASIN)
    parent_units = set()
    brands = set()
    covered_cat_map: Dict[str, Dict[str, Any]] = {}
    garment_types = set()
    genders = set()
    scores: List[float] = []
    normal_scores: List[float] = []
    anomalous_asins: List[str] = []
    anomalous_items: List[Dict[str, Any]] = []
    anomalous_scored_count = 0
    sales_list: List[float] = []
    prices: List[float] = []

    for p in items:
        asin = str(p.get("asin") or "").strip().upper()
        parent = str(p.get("parent_asin") or "").strip().upper()
        parent_units.add(parent if parent else asin)

        brand = str(p.get("brand") or "").strip()
        if brand and brand.lower() not in ("unknown", "generic", "n/a"):
            brands.add(brand.lower())
        elif brand:
            brands.add(brand.lower())

        for src in p.get("source_categories") or []:
            ckey = str(src.get("node_id") or src.get("category_key") or src.get("source_category") or "")
            if ckey and ckey not in covered_cat_map:
                covered_cat_map[ckey] = {
                    "node_id": src.get("node_id"),
                    "category_key": src.get("category_key"),
                    "name": src.get("source_category") or src.get("name") or ckey,
                    "short_name": src.get("short_name") or src.get("source_category") or ckey,
                    "garment_type": src.get("garment_type"),
                    "gender": src.get("gender"),
                    "color": src.get("color") or "#3B6BF6",
                }
            if src.get("garment_type"):
                garment_types.add(src["garment_type"])
            if src.get("gender"):
                genders.add(src["gender"])

        for gt in p.get("garment_types") or []:
            if gt:
                garment_types.add(gt)
        for gd in p.get("genders") or []:
            if gd:
                genders.add(gd)

        raw_price = p.get("price")
        if raw_price is None:
            raw_price = p.get("sales_price_usd")
        price_valid = p.get("price_valid")
        if price_valid is None:
            price_valid = raw_price is not None and float(raw_price) > 0
        if price_valid and raw_price is not None and float(raw_price) > 0:
            prices.append(float(raw_price))

        is_p_anom = bool(p.get("price_anomaly")) or (
            bool(price_valid) and is_price_anomaly(raw_price)
        )
        is_s_susp = bool(p.get("sales_suspect"))
        is_anom = is_p_anom or is_s_susp

        if is_anom:
            anomalous_asins.append(asin)
            anomalous_items.append(
                {
                    "asin": asin,
                    "price_anomaly": is_p_anom,
                    "sales_suspect": is_s_susp,
                    "total_score": p.get("total_score"),
                }
            )

        sc = p.get("total_score")
        if sc is not None:
            sc_val = float(sc)
            scores.append(sc_val)
            if is_anom:
                anomalous_scored_count += 1
            else:
                normal_scores.append(sc_val)

        s30 = p.get("listing_sales_30d")
        if s30 is not None and float(s30) > 0:
            sales_list.append(float(s30))

    parent_asin_count = len(parent_units)
    brand_count = len(brands)
    raw_peak_score = max(scores) if scores else None
    if normal_scores:
        peak_score = max(normal_scores)
        all_anomalous = False
        excluded_anomaly_count = len(anomalous_asins)
    elif scores:
        # Fallback: every scored product in this direction is anomalous -> use raw max & warn UI
        peak_score = max(scores)
        all_anomalous = True
        excluded_anomaly_count = 0
    else:
        peak_score = 0.0
        all_anomalous = len(anomalous_asins) == asin_count and asin_count > 0
        excluded_anomaly_count = 0
    max_sales = int(round(max(sales_list))) if sales_list else None
    median_sales = int(round(_median(sales_list))) if sales_list else None
    price_range = (
        {"min": round(min(prices), 2), "max": round(max(prices), 2)}
        if prices
        else None
    )

    # Direction Composite Recommendation Score (explainable)
    base_score = peak_score
    breadth_bonus = min(20.0, 10.0 * math.log(max(1, parent_asin_count)))

    covers_both_garments = {"t_shirt", "sweatshirt"}.issubset(garment_types)
    covers_both_genders = {"women", "men"}.issubset(genders)
    if covers_both_garments and covers_both_genders:
        cross_bonus = 8.0
        cross_reason = "跨服装类型（T恤+卫衣）且跨性别（女装+男装）"
    elif covers_both_garments:
        cross_bonus = 8.0
        cross_reason = "跨服装类型（同时覆盖 T恤 与 卫衣）"
    elif covers_both_genders:
        cross_bonus = 8.0
        cross_reason = "跨性别受众（同时覆盖 女装 与 男装）"
    else:
        cross_bonus = 0.0
        cross_reason = "单服装类型且单性别"

    feas_delta = (feas - 3) * 3.0
    direction_score = round(base_score + breadth_bonus + cross_bonus + feas_delta, 1)

    return {
        "asin_count": asin_count,
        "parent_asin_count": parent_asin_count,
        "brand_count": brand_count,
        "covered_categories": list(covered_cat_map.values()),
        "garment_types": sorted(garment_types),
        "genders": sorted(genders),
        "peak_product_score": round(peak_score, 1) if scores else None,
        "raw_peak_product_score": round(raw_peak_score, 1) if raw_peak_score is not None else None,
        "all_anomalous": all_anomalous,
        "excluded_anomaly_count": excluded_anomaly_count,
        "anomalous_asins": anomalous_asins,
        "anomalous_items": anomalous_items,
        "max_sales_30d": max_sales,
        "median_sales_30d": median_sales,
        "price_range": price_range,
        "direction_score": direction_score,
        "score_breakdown": {
            "base_score": round(base_score, 1),
            "raw_peak_score": round(raw_peak_score, 1) if raw_peak_score is not None else None,
            "all_anomalous": all_anomalous,
            "excluded_anomaly_count": excluded_anomaly_count,
            "anomalous_asins": anomalous_asins,
            "breadth_bonus": round(breadth_bonus, 1),
            "cross_category_bonus": round(cross_bonus, 1),
            "cross_category_reason": cross_reason,
            "feasibility_delta": round(feas_delta, 1),
            "total": direction_score,
        },
    }


class DirectionsService:
    """Manages Stage 05 print directions, ASIN links, candidate pools, and real-time aggregation."""

    def __init__(
        self,
        scoring: ScoringService,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
    ):
        self.scoring = scoring
        self.preprocessing = scoring.preprocessing
        self.app_db_path = app_db_path
        self._lock = threading.Lock()
        self._init_db()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.app_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.app_db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(DIRECTIONS_SCHEMA_SQL)
                # Idempotent schema migration for pre-existing databases
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(print_directions)").fetchall()
                }
                if "direction_type" not in cols:
                    conn.execute(
                        "ALTER TABLE print_directions ADD COLUMN direction_type TEXT NOT NULL DEFAULT 'trend_cluster'"
                    )
                if "source" not in cols:
                    conn.execute(
                        "ALTER TABLE print_directions ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                    )
                if "variant_suggestions" not in cols:
                    conn.execute(
                        "ALTER TABLE print_directions ADD COLUMN variant_suggestions TEXT NOT NULL DEFAULT '[]'"
                    )
                conn.commit()

    def _resolve_preset_params(
        self, preset_id: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str, Dict[str, Any]]:
        """Resolve scoring weights/price ranges for Stage 05/06.

        - When `preset_id` explicitly names a saved preset, use that preset.
        - Otherwise (None / "" / "active"), automatically follow Stage 04's persisted
          `scoring_active_config` so custom slider adjustments flow live into 05 & 06.
        """
        pid = str(preset_id or "").strip()
        if pid and pid != "active":
            presets = self.scoring.list_presets()
            found = next((p for p in presets if p["preset_id"] == pid), None)
            if found:
                cfg = self.scoring._serialize_active_config(
                    preset_id=found["preset_id"],
                    preset_name=found["name"],
                    weights=found["weights"],
                    price_ranges=found["price_ranges"],
                    updated_at=found.get("updated_at"),
                )
                return found["weights"], found["price_ranges"], found["preset_id"], cfg

        active = self.scoring.get_active_config()
        return active["weights"], active["price_ranges"], active["preset_id"], active

    def _load_link_maps(self) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
        """Load all direction <-> ASIN links indexed both by direction_id and by ASIN."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT l.direction_id, l.asin, l.added_at, d.name, d.status
                   FROM direction_asin_links l
                   JOIN print_directions d ON d.id = l.direction_id
                   ORDER BY l.added_at DESC, l.id DESC"""
            ).fetchall()

        by_dir: Dict[int, List[Dict[str, Any]]] = {}
        by_asin: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            did = int(r["direction_id"])
            asin = r["asin"]
            by_dir.setdefault(did, []).append({"asin": asin, "added_at": r["added_at"]})
            by_asin.setdefault(asin, []).append(
                {
                    "id": did,
                    "name": r["name"],
                    "status": r["status"],
                    "added_at": r["added_at"],
                }
            )
        return by_dir, by_asin

    def _build_product_lookup(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Evaluate Stage 04 full ready pool and build lookup maps (scored + hydrated fallback)."""
        weights, price_ranges, active_pid, active_cfg = self._resolve_preset_params(preset_id)
        eval_res = self.scoring.evaluate(
            observed_date=observed_date,
            weights=weights,
            price_ranges=price_ranges,
        )
        eval_res["active_preset_id"] = active_pid
        eval_res["active_scoring_config"] = active_cfg
        scored_map: Dict[str, Dict[str, Any]] = {
            item["asin"]: item for item in eval_res.get("items") or []
        }

        fallback_map: Dict[str, Dict[str, Any]] = {}
        if eval_res.get("ready") and eval_res.get("selected_date"):
            hydrated = self.preprocessing._hydrate_rows(eval_res["selected_date"])
            for h in hydrated:
                fallback_map[h["asin"]] = h
        return eval_res, scored_map, fallback_map

    def _hydrate_direction_row(
        self,
        row: sqlite3.Row,
        links: List[Dict[str, Any]],
        scored_map: Dict[str, Dict[str, Any]],
        fallback_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        linked_products: List[Dict[str, Any]] = []
        for lk in links:
            asin = lk["asin"]
            if asin in scored_map:
                sp = scored_map[asin]
                fb = fallback_map.get(asin) or {}
                srcs = sp.get("source_categories") or []
                img = sp.get("image_url") or ""
                amz_url = fb.get("amazon_url") or (
                    f"https://www.amazon.com/dp/{asin}"
                    if sp.get("parent_asin") and sp.get("parent_asin") != asin
                    else f"https://www.amazon.com/dp/{asin}"
                )
                amz_verified = bool(
                    fb.get("amazon_url_verified")
                    if "amazon_url_verified" in fb
                    else (sp.get("parent_asin") and sp.get("parent_asin") != asin)
                )
                linked_products.append(
                    {
                        "asin": asin,
                        "parent_asin": sp.get("parent_asin"),
                        "title": sp.get("title") or "",
                        "brand": sp.get("brand") or "",
                        "image_url": img,
                        "main_image_url": img,
                        "amazon_url": amz_url,
                        "amazon_url_verified": amz_verified,
                        "photos": fb.get("photos") or ([img] if img else []),
                        "source_categories": srcs,
                        "covered_categories": [
                            s.get("source_category") or s.get("name") or s.get("node_id")
                            for s in srcs
                        ],
                        "garment_types": sp.get("garment_types") or [],
                        "genders": sp.get("genders") or [],
                        "listing_sales_30d": sp.get("listing_sales_30d"),
                        "price": sp.get("price"),
                        "sales_price_usd": sp.get("price"),
                        "price_valid": sp.get("price_valid", False),
                        "price_anomaly": bool(sp.get("price_anomaly")),
                        "sales_suspect": bool(sp.get("sales_suspect")),
                        "sales_suspect_details": list(sp.get("sales_suspect_details") or []),
                        "rating_stars": sp.get("rating_stars"),
                        "ratings_count": sp.get("ratings_count"),
                        "total_score": sp.get("total_score"),
                        "rank": sp.get("rank"),
                        "confidence_score": sp.get("confidence_score"),
                        "confidence_level": sp.get("confidence_level"),
                        "confidence_label": sp.get("confidence_label"),
                        "confidence_penalties": list(sp.get("confidence_penalties") or []),
                        "ai_analysis": fb.get("ai_analysis"),
                        "in_ready_pool": True,
                        "added_at": lk["added_at"],
                    }
                )
            elif asin in fallback_map:
                fb = fallback_map[asin]
                srcs = fb.get("source_categories") or []
                img = fb.get("image_url") or ""
                conf = compute_confidence(fb)
                linked_products.append(
                    {
                        "asin": asin,
                        "parent_asin": fb.get("parent_asin"),
                        "title": fb.get("title") or "",
                        "brand": fb.get("brand") or "",
                        "image_url": img,
                        "main_image_url": img,
                        "amazon_url": fb.get("amazon_url") or f"https://www.amazon.com/dp/{asin}",
                        "amazon_url_verified": bool(fb.get("amazon_url_verified", False)),
                        "photos": fb.get("photos") or ([img] if img else []),
                        "source_categories": srcs,
                        "covered_categories": [
                            s.get("source_category") or s.get("name") or s.get("node_id")
                            for s in srcs
                        ],
                        "garment_types": fb.get("garment_types") or [],
                        "genders": fb.get("genders") or [],
                        "listing_sales_30d": fb.get("listing_sales_30d"),
                        "price": fb.get("price"),
                        "sales_price_usd": fb.get("price"),
                        "price_valid": fb.get("price_valid", False),
                        "price_anomaly": bool(fb.get("price_anomaly")),
                        "sales_suspect": bool(fb.get("sales_suspect")),
                        "sales_suspect_details": list(fb.get("sales_suspect_details") or []),
                        "rating_stars": fb.get("rating_stars"),
                        "ratings_count": fb.get("ratings_count"),
                        "total_score": None,
                        "rank": None,
                        "confidence_score": conf["score"],
                        "confidence_level": conf["level"],
                        "confidence_label": conf["label"],
                        "confidence_penalties": conf["penalties"],
                        "ai_analysis": fb.get("ai_analysis"),
                        "in_ready_pool": False,
                        "added_at": lk["added_at"],
                    }
                )
            else:
                linked_products.append(
                    {
                        "asin": asin,
                        "parent_asin": None,
                        "title": f"（历史快照商品 {asin}）",
                        "brand": "",
                        "image_url": "",
                        "main_image_url": "",
                        "amazon_url": f"https://www.amazon.com/dp/{asin}",
                        "amazon_url_verified": False,
                        "photos": [],
                        "source_categories": [],
                        "covered_categories": [],
                        "garment_types": [],
                        "genders": [],
                        "listing_sales_30d": None,
                        "price": None,
                        "sales_price_usd": None,
                        "price_valid": False,
                        "price_anomaly": False,
                        "sales_suspect": False,
                        "sales_suspect_details": [],
                        "total_score": None,
                        "rank": None,
                        "confidence_score": 50,
                        "confidence_level": "low",
                        "confidence_label": "低",
                        "confidence_penalties": [],
                        "in_ready_pool": False,
                        "added_at": lk["added_at"],
                    }
                )

        linked_products.sort(
            key=lambda p: (
                -(p["total_score"] if p["total_score"] is not None else -1.0),
                -(p["listing_sales_30d"] if p["listing_sales_30d"] is not None else -1),
                p["asin"],
            )
        )

        metrics = aggregate_direction_metrics(
            linked_products,
            feasibility_rating=int(row["feasibility_rating"]),
        )
        preview_images = [
            {"asin": p["asin"], "image_url": p["image_url"], "title": p["title"]}
            for p in linked_products
            if p.get("image_url")
        ][:4]

        row_keys = set(row.keys())
        direction_type = (
            str(row["direction_type"] or "trend_cluster").strip().lower()
            if "direction_type" in row_keys
            else "trend_cluster"
        )
        if direction_type not in DIRECTION_TYPES:
            direction_type = "trend_cluster"

        source = (
            str(row["source"] or "manual").strip().lower()
            if "source" in row_keys
            else "manual"
        )
        if source not in DIRECTION_SOURCES:
            source = "manual"

        raw_variants = row["variant_suggestions"] if "variant_suggestions" in row_keys else "[]"
        variant_suggestions = normalize_variant_suggestions(raw_variants)
        season_info = compute_direction_season(linked_products)

        return {
            "id": int(row["id"]),
            "name": row["name"],
            "style_tags": normalize_style_tags(row["style_tags"]),
            "target_audience": row["target_audience"] or "",
            "feasibility_rating": int(row["feasibility_rating"]),
            "notes": row["notes"] or "",
            "status": row["status"],
            "direction_type": direction_type,
            "direction_type_label": DIRECTION_TYPE_LABELS.get(direction_type, direction_type),
            "source": source,
            "source_label": DIRECTION_SOURCE_LABELS.get(source, source),
            "season_tag": season_info["season_tag"],
            "season_label": season_info["season_label"],
            "is_seasonal": season_info["is_seasonal"],
            "season_bucket": season_info["season_bucket"],
            "holiday_date": season_info["holiday_date"],
            "days_until_holiday": season_info["days_until_holiday"],
            "season_votes": season_info["season_votes"],
            "variant_suggestions": variant_suggestions,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metrics": metrics,
            "direction_score": metrics["direction_score"],
            "all_anomalous": metrics["all_anomalous"],
            "excluded_anomaly_count": metrics["excluded_anomaly_count"],
            "anomalous_asins": metrics["anomalous_asins"],
            "preview_images": preview_images,
            "linked_products": linked_products,
        }

    def _compute_standalone_candidates(
        self,
        full_ranked: List[Dict[str, Any]],
        top_slice: List[Dict[str, Any]],
        by_asin: Dict[str, List[Dict[str, Any]]],
        fallback_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Identify strong ungrouped standalone hit candidates (Top 20 score OR Top 20 30d sales)."""
        score_rank_map = {
            item["asin"]: int(item.get("rank") or idx)
            for idx, item in enumerate(full_ranked, start=1)
        }
        sales_ranked = sorted(
            [item for item in full_ranked if item.get("listing_sales_30d") is not None],
            key=lambda i: (-(i.get("listing_sales_30d") or 0), -(i.get("total_score") or 0.0), i["asin"]),
        )
        sales_rank_map = {item["asin"]: idx for idx, item in enumerate(sales_ranked, start=1)}

        score_top20 = {item["asin"] for item in full_ranked[:20]}
        sales_top20 = {item["asin"] for item in sales_ranked[:20]}

        standalone: List[Dict[str, Any]] = []
        for item in top_slice:
            asin = item["asin"]
            if by_asin.get(asin):
                continue
            in_score_top20 = asin in score_top20
            in_sales_top20 = asin in sales_top20
            if not (in_score_top20 or in_sales_top20):
                continue

            fb = fallback_map.get(asin) or {}
            ai_info = fb.get("ai_analysis")
            reasons: List[str] = []
            if in_score_top20:
                reasons.append(f"04 潜力分 Top 20 (#{score_rank_map.get(asin)})")
            if in_sales_top20:
                reasons.append(f"30天销量 Top 20 (#{sales_rank_map.get(asin)})")

            suggested_name = ""
            suggested_tags: List[str] = []
            if ai_info:
                suggested_name = (ai_info.get("print_subject") or "").strip()[:48]
                suggested_tags = list(ai_info.get("theme_tags") or [])
                if ai_info.get("visual_style") and ai_info["visual_style"] not in suggested_tags:
                    suggested_tags.insert(0, ai_info["visual_style"])
                suggested_tags = suggested_tags[:4]
            if not suggested_name:
                raw_t = (item.get("title") or "").strip()
                suggested_name = (raw_t[:36] + "…") if len(raw_t) > 36 else (raw_t or f"独立爆款 {asin}")

            standalone.append(
                {
                    **item,
                    "photos": fb.get("photos") or ([item["image_url"]] if item.get("image_url") else []),
                    "ai_analysis": ai_info,
                    "sales_rank": sales_rank_map.get(asin),
                    "standalone_reasons": reasons,
                    "suggested_name": suggested_name,
                    "suggested_tags": suggested_tags,
                }
            )

        standalone.sort(
            key=lambda x: (
                -(x.get("total_score") or 0.0),
                -(x.get("listing_sales_30d") or 0),
                x["asin"],
            )
        )
        return standalone

    def get_candidates(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
        top_n: int = 80,
        category: str = "all",
        assigned: str = "all",
        q: str = "",
    ) -> Dict[str, Any]:
        """Return Top-N Stage 04 candidates annotated with `assigned_directions`."""
        safe_top_n = max(10, min(500, int(top_n or 80)))
        eval_res, _, fallback_map = self._build_product_lookup(
            observed_date=observed_date,
            preset_id=preset_id,
        )
        if not eval_res.get("ready"):
            return {
                "ready": False,
                "db_status": eval_res.get("db_status"),
                "selected_date": observed_date,
                "top_n": safe_top_n,
                "active_preset_id": eval_res.get("active_preset_id", "system_default"),
                "presets": eval_res.get("presets") or [],
                "categories": serialize_categories(),
                "counts": {"total_in_pool": 0, "top_n_count": 0, "unassigned_count": 0, "assigned_count": 0, "standalone_count": 0},
                "items": [],
                "standalone_candidates": [],
            }

        _, by_asin = self._load_link_maps()
        full_ranked = eval_res.get("items") or []
        top_slice = full_ranked[:safe_top_n]
        standalone_candidates = self._compute_standalone_candidates(
            full_ranked, top_slice, by_asin, fallback_map
        )

        annotated: List[Dict[str, Any]] = []
        for item in top_slice:
            asin = item["asin"]
            fb = fallback_map.get(asin) or {}
            assigned_dirs = by_asin.get(asin, [])
            annotated.append(
                {
                    **item,
                    "photos": fb.get("photos") or ([item["image_url"]] if item.get("image_url") else []),
                    "ai_analysis": fb.get("ai_analysis"),
                    "assigned_directions": assigned_dirs,
                    "is_assigned": len(assigned_dirs) > 0,
                }
            )

        # Category filter before counting assigned/unassigned within chosen category
        cat_filtered = annotated
        if category and category != "all":
            spec = resolve_category(category)
            target_node = spec.node_id if (spec and spec.is_configured and spec.node_id) else category
            cat_filtered = [
                i for i in cat_filtered
                if any(
                    s.get("node_id") == target_node or s.get("category_key") == category
                    for s in i.get("source_categories") or []
                )
            ]

        query = (q or "").strip().lower()
        if query:
            cat_filtered = [
                i for i in cat_filtered
                if query in i["asin"].lower()
                or query in (i.get("title") or "").lower()
                or query in (i.get("brand") or "").lower()
            ]

        unassigned_count = sum(1 for i in cat_filtered if not i["is_assigned"])
        assigned_count = len(cat_filtered) - unassigned_count

        assigned_mode = (assigned or "all").strip().lower()
        if assigned_mode == "unassigned":
            final_items = [i for i in cat_filtered if not i["is_assigned"]]
        elif assigned_mode == "assigned":
            final_items = [i for i in cat_filtered if i["is_assigned"]]
        else:
            final_items = cat_filtered

        return {
            "ready": True,
            "selected_date": eval_res.get("selected_date"),
            "available_dates": eval_res.get("available_dates") or [],
            "top_n": safe_top_n,
            "active_preset_id": eval_res.get("active_preset_id", "system_default"),
            "active_scoring_config": eval_res.get("active_scoring_config"),
            "presets": eval_res.get("presets") or [],
            "categories": serialize_categories(),
            "category_filter": category,
            "assigned_filter": assigned_mode,
            "counts": {
                "total_in_pool": len(full_ranked),
                "top_n_count": len(cat_filtered),
                "unassigned_count": unassigned_count,
                "assigned_count": assigned_count,
                "standalone_count": len(standalone_candidates),
            },
            "items": final_items,
            "standalone_candidates": standalone_candidates,
        }

    def list_directions(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
        sort: str = "direction_score",
        status: str = "all",
    ) -> Dict[str, Any]:
        eval_res, scored_map, fallback_map = self._build_product_lookup(
            observed_date=observed_date,
            preset_id=preset_id,
        )
        by_dir, _ = self._load_link_maps()

        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, name, style_tags, target_audience, feasibility_rating,
                          notes, status, direction_type, source, variant_suggestions, created_at, updated_at
                   FROM print_directions
                   ORDER BY id DESC"""
            ).fetchall()

        directions = [
            self._hydrate_direction_row(r, by_dir.get(int(r["id"]), []), scored_map, fallback_map)
            for r in rows
        ]

        status_counts = {"all": len(directions), "draft": 0, "shortlisted": 0, "archived": 0}
        for d in directions:
            if d["status"] in status_counts:
                status_counts[d["status"]] += 1

        st_filter = (status or "all").strip().lower()
        if st_filter in DIRECTION_STATUSES:
            directions = [d for d in directions if d["status"] == st_filter]

        sort_key = (sort or "direction_score").strip().lower()
        if sort_key == "asin_count":
            directions.sort(
                key=lambda d: (-d["metrics"]["asin_count"], -d["direction_score"], -d["id"])
            )
        elif sort_key == "created_at":
            directions.sort(key=lambda d: (d["created_at"], d["id"]), reverse=True)
        else:
            directions.sort(
                key=lambda d: (
                    0 if d["status"] == "shortlisted" else (1 if d["status"] == "draft" else 2),
                    -d["direction_score"],
                    -d["metrics"]["parent_asin_count"],
                    -d["id"],
                )
            )

        return {
            "ready": bool(eval_res.get("ready")),
            "selected_date": eval_res.get("selected_date"),
            "active_preset_id": eval_res.get("active_preset_id", "system_default"),
            "active_scoring_config": eval_res.get("active_scoring_config"),
            "sort": sort_key,
            "status_counts": status_counts,
            "items": directions,
        }

    def get_direction(
        self,
        direction_id: int,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, name, style_tags, target_audience, feasibility_rating,
                          notes, status, direction_type, source, variant_suggestions, created_at, updated_at
                   FROM print_directions WHERE id = ?""",
                (int(direction_id),),
            ).fetchone()
        if row is None:
            raise DirectionNotFoundError(f"未找到印花方向 ID：{direction_id}。")

        _, scored_map, fallback_map = self._build_product_lookup(
            observed_date=observed_date,
            preset_id=preset_id,
        )
        by_dir, _ = self._load_link_maps()
        return self._hydrate_direction_row(
            row, by_dir.get(int(direction_id), []), scored_map, fallback_map
        )

    def create_direction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise DirectionValidationError("请求体必须为 JSON 对象。")

        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 64:
            raise DirectionValidationError("方向主题名称不能为空且不超过 64 个字符。")

        style_tags = normalize_style_tags(payload.get("style_tags"))
        target_audience = str(payload.get("target_audience") or "").strip()[:80]
        notes = str(payload.get("notes") or "").strip()[:1000]

        raw_feas = payload.get("feasibility_rating", 3)
        try:
            feas = int(raw_feas)
        except (TypeError, ValueError) as exc:
            raise DirectionValidationError("可行性评分必须为 1~5 的整数。") from exc
        if not 1 <= feas <= 5:
            raise DirectionValidationError("可行性评分必须在 1 到 5 之间。")

        status = str(payload.get("status") or "draft").strip().lower()
        if status not in DIRECTION_STATUSES:
            raise DirectionValidationError("方向状态仅支持 draft、shortlisted 或 archived。")

        direction_type = str(payload.get("direction_type") or "trend_cluster").strip().lower()
        if direction_type not in DIRECTION_TYPES:
            raise DirectionValidationError("方向类型仅支持 trend_cluster 或 standalone_hit。")

        source = str(payload.get("source") or "manual").strip().lower()
        if source not in DIRECTION_SOURCES:
            raise DirectionValidationError("方向来源仅支持 manual 或 ai_suggested。")

        initial_asins = normalize_asins(payload.get("asins"))
        variant_suggestions = normalize_variant_suggestions(payload.get("variant_suggestions"))
        observed_date = payload.get("observed_date")
        preset_id = payload.get("preset_id")
        now = self._now_iso()

        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO print_directions
                       (name, style_tags, target_audience, feasibility_rating, notes, status,
                        direction_type, source, variant_suggestions, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        name,
                        json.dumps(style_tags, ensure_ascii=False),
                        target_audience,
                        feas,
                        notes,
                        status,
                        direction_type,
                        source,
                        json.dumps(variant_suggestions, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                direction_id = int(cur.lastrowid)
                for asin in initial_asins:
                    conn.execute(
                        """INSERT OR IGNORE INTO direction_asin_links (direction_id, asin, added_at)
                           VALUES (?, ?, ?)""",
                        (direction_id, asin, now),
                    )
                conn.commit()

        return {
            "direction": self.get_direction(
                direction_id, observed_date=observed_date, preset_id=preset_id
            ),
            "added_count": len(initial_asins),
        }

    def update_direction(self, direction_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise DirectionValidationError("请求体必须为 JSON 对象。")

        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM print_directions WHERE id = ?",
                    (int(direction_id),),
                ).fetchone()
                if existing is None:
                    raise DirectionNotFoundError(f"未找到印花方向 ID：{direction_id}。")

                name = existing["name"]
                if "name" in payload:
                    name = str(payload.get("name") or "").strip()
                    if not name or len(name) > 64:
                        raise DirectionValidationError("方向主题名称不能为空且不超过 64 个字符。")

                style_tags_json = existing["style_tags"]
                if "style_tags" in payload:
                    style_tags_json = json.dumps(
                        normalize_style_tags(payload.get("style_tags")),
                        ensure_ascii=False,
                    )

                target_audience = existing["target_audience"]
                if "target_audience" in payload:
                    target_audience = str(payload.get("target_audience") or "").strip()[:80]

                notes = existing["notes"]
                if "notes" in payload:
                    notes = str(payload.get("notes") or "").strip()[:1000]

                feas = int(existing["feasibility_rating"])
                if "feasibility_rating" in payload:
                    try:
                        feas = int(payload.get("feasibility_rating"))
                    except (TypeError, ValueError) as exc:
                        raise DirectionValidationError("可行性评分必须为 1~5 的整数。") from exc
                    if not 1 <= feas <= 5:
                        raise DirectionValidationError("可行性评分必须在 1 到 5 之间。")

                status = existing["status"]
                if "status" in payload:
                    status = str(payload.get("status") or "").strip().lower()
                    if status not in DIRECTION_STATUSES:
                        raise DirectionValidationError("方向状态仅支持 draft、shortlisted 或 archived。")

                direction_type = (
                    existing["direction_type"]
                    if "direction_type" in existing.keys()
                    else "trend_cluster"
                )
                if "direction_type" in payload:
                    direction_type = str(payload.get("direction_type") or "").strip().lower()
                    if direction_type not in DIRECTION_TYPES:
                        raise DirectionValidationError("方向类型仅支持 trend_cluster 或 standalone_hit。")

                variant_suggestions_json = (
                    existing["variant_suggestions"]
                    if "variant_suggestions" in existing.keys()
                    else "[]"
                )
                if "variant_suggestions" in payload:
                    variant_suggestions_json = json.dumps(
                        normalize_variant_suggestions(payload.get("variant_suggestions")),
                        ensure_ascii=False,
                    )

                now = self._now_iso()
                conn.execute(
                    """UPDATE print_directions
                       SET name = ?, style_tags = ?, target_audience = ?,
                           feasibility_rating = ?, notes = ?, status = ?, direction_type = ?,
                           variant_suggestions = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        name,
                        style_tags_json,
                        target_audience,
                        feas,
                        notes,
                        status,
                        direction_type,
                        variant_suggestions_json,
                        now,
                        int(direction_id),
                    ),
                )
                conn.commit()

        return {
            "direction": self.get_direction(
                direction_id,
                observed_date=payload.get("observed_date"),
                preset_id=payload.get("preset_id"),
            )
        }

    def delete_direction(self, direction_id: int) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id, name FROM print_directions WHERE id = ?",
                    (int(direction_id),),
                ).fetchone()
                if existing is None:
                    raise DirectionNotFoundError(f"未找到印花方向 ID：{direction_id}。")
                conn.execute("BEGIN IMMEDIATE")
                removed_links = conn.execute(
                    "DELETE FROM direction_asin_links WHERE direction_id = ?",
                    (int(direction_id),),
                ).rowcount
                conn.execute(
                    "DELETE FROM print_directions WHERE id = ?",
                    (int(direction_id),),
                )
                conn.commit()
        return {
            "deleted_id": int(direction_id),
            "deleted_name": existing["name"],
            "removed_links_count": int(removed_links),
        }

    def add_links(
        self,
        direction_id: int,
        asins_raw: Any,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        asins = normalize_asins(asins_raw)
        if not asins:
            raise DirectionValidationError("请至少选择 1 个商品 ASIN 进行归入。")

        now = self._now_iso()
        added: List[str] = []
        duplicates: List[str] = []

        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM print_directions WHERE id = ?",
                    (int(direction_id),),
                ).fetchone()
                if existing is None:
                    raise DirectionNotFoundError(f"未找到印花方向 ID：{direction_id}。")

                conn.execute("BEGIN IMMEDIATE")
                for asin in asins:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO direction_asin_links (direction_id, asin, added_at)
                           VALUES (?, ?, ?)""",
                        (int(direction_id), asin, now),
                    )
                    if cur.rowcount > 0:
                        added.append(asin)
                    else:
                        duplicates.append(asin)
                conn.execute(
                    "UPDATE print_directions SET updated_at = ? WHERE id = ?",
                    (now, int(direction_id)),
                )
                conn.commit()

        return {
            "direction_id": int(direction_id),
            "added_count": len(added),
            "added_asins": added,
            "duplicate_count": len(duplicates),
            "duplicate_asins": duplicates,
            "direction": self.get_direction(
                direction_id, observed_date=observed_date, preset_id=preset_id
            ),
        }

    def remove_links(
        self,
        direction_id: int,
        asins_raw: Any,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        asins = normalize_asins(asins_raw)
        if not asins:
            raise DirectionValidationError("请至少提供 1 个需要移出的商品 ASIN。")

        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM print_directions WHERE id = ?",
                    (int(direction_id),),
                ).fetchone()
                if existing is None:
                    raise DirectionNotFoundError(f"未找到印花方向 ID：{direction_id}。")

                placeholders = ",".join("?" for _ in asins)
                conn.execute("BEGIN IMMEDIATE")
                removed_count = conn.execute(
                    f"""DELETE FROM direction_asin_links
                        WHERE direction_id = ? AND asin IN ({placeholders})""",
                    (int(direction_id), *asins),
                ).rowcount
                conn.execute(
                    "UPDATE print_directions SET updated_at = ? WHERE id = ?",
                    (now, int(direction_id)),
                )
                conn.commit()

        return {
            "direction_id": int(direction_id),
            "removed_count": int(removed_count),
            "direction": self.get_direction(
                direction_id, observed_date=observed_date, preset_id=preset_id
            ),
        }

    def ai_cluster_candidates(
        self,
        ai_service: Any,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
        top_n: int = 60,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Stage 05 Map-Reduce AI clustering + Standalone Hits fallback.

        - Preserves user manual & shortlisted directions strictly (never overwrites).
        - Saves AI trend clusters as `status='draft'`, `source='ai_suggested'`, `direction_type='trend_cluster'`.
        - Allows multi-label (1 ASIN in 1~3 clusters) and rejects catch-all/misc buckets.
        - Returns ungrouped strong singles (Top 20 score or Top 20 30d sales) in `standalone_candidates`.
        """
        safe_top_n = max(10, min(120, int(top_n or 60)))
        eval_res, scored_map, fallback_map = self._build_product_lookup(
            observed_date=observed_date,
            preset_id=preset_id,
        )
        if not eval_res.get("ready"):
            raise DirectionValidationError("当前无可用的 04 评分候选池，无法执行 AI 归纳分组。")

        full_ranked = eval_res.get("items") or []
        top_slice = full_ranked[:safe_top_n]
        if not top_slice:
            raise DirectionValidationError("当前 04 评分候选池为空，请先完成 02 数据预处理。")

        cluster_res = ai_service.cluster_candidates(top_slice, dry_run=dry_run)
        if dry_run:
            return {
                **cluster_res,
                "top_n": safe_top_n,
                "selected_date": eval_res.get("selected_date"),
                "active_preset_id": eval_res.get("active_preset_id", "system_default"),
            }

        now = self._now_iso()
        created_ids: List[int] = []
        updated_ids: List[int] = []
        skipped_existing: List[Dict[str, Any]] = []

        with self._lock:
            with self._connect() as conn:
                existing_rows = conn.execute(
                    "SELECT id, name, status, source, direction_type FROM print_directions"
                ).fetchall()
                by_name = {str(r["name"] or "").strip().lower(): dict(r) for r in existing_rows}

                conn.execute("BEGIN IMMEDIATE")
                for cl in cluster_res.get("clusters") or []:
                    name = str(cl.get("name") or "").strip()[:64]
                    if not name:
                        continue
                    name_key = name.lower()
                    tags = normalize_style_tags(cl.get("style_tags"))
                    audience = str(cl.get("target_audience") or "").strip()[:80]
                    notes = str(cl.get("notes") or "").strip()[:1000]
                    variants = normalize_variant_suggestions(cl.get("variant_suggestions"))
                    asins = normalize_asins(cl.get("asins"))
                    if len(asins) < 2:
                        continue

                    existing = by_name.get(name_key)
                    if existing is not None:
                        # Strictly protect manual directions and any shortlisted direction
                        if existing["source"] == "manual" or existing["status"] == "shortlisted":
                            skipped_existing.append(
                                {
                                    "id": int(existing["id"]),
                                    "name": existing["name"],
                                    "reason": "已存在人工创建或已入选方向，未覆盖",
                                }
                            )
                            continue
                        # Refresh an existing un-shortlisted AI draft without losing links
                        dir_id = int(existing["id"])
                        if variants:
                            conn.execute(
                                """UPDATE print_directions
                                   SET style_tags = ?, target_audience = ?, notes = ?, variant_suggestions = ?, updated_at = ?
                                   WHERE id = ?""",
                                (
                                    json.dumps(tags, ensure_ascii=False),
                                    audience,
                                    notes,
                                    json.dumps(variants, ensure_ascii=False),
                                    now,
                                    dir_id,
                                ),
                            )
                        else:
                            conn.execute(
                                """UPDATE print_directions
                                   SET style_tags = ?, target_audience = ?, notes = ?, updated_at = ?
                                   WHERE id = ?""",
                                (
                                    json.dumps(tags, ensure_ascii=False),
                                    audience,
                                    notes,
                                    now,
                                    dir_id,
                                ),
                            )
                        for asin in asins:
                            conn.execute(
                                """INSERT OR IGNORE INTO direction_asin_links (direction_id, asin, added_at)
                                   VALUES (?, ?, ?)""",
                                (dir_id, asin, now),
                            )
                        updated_ids.append(dir_id)
                    else:
                        cur = conn.execute(
                            """INSERT INTO print_directions
                               (name, style_tags, target_audience, feasibility_rating, notes, status,
                                direction_type, source, variant_suggestions, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, 'draft', 'trend_cluster', 'ai_suggested', ?, ?, ?)""",
                            (
                                name,
                                json.dumps(tags, ensure_ascii=False),
                                audience,
                                3,
                                notes,
                                json.dumps(variants, ensure_ascii=False),
                                now,
                                now,
                            ),
                        )
                        dir_id = int(cur.lastrowid)
                        for asin in asins:
                            conn.execute(
                                """INSERT OR IGNORE INTO direction_asin_links (direction_id, asin, added_at)
                                   VALUES (?, ?, ?)""",
                                (dir_id, asin, now),
                            )
                        created_ids.append(dir_id)
                        by_name[name_key] = {
                            "id": dir_id,
                            "name": name,
                            "status": "draft",
                            "source": "ai_suggested",
                            "direction_type": "trend_cluster",
                        }
                conn.commit()

        # Reload hydrated fallback_map so freshly cached AI visual fields are visible
        if eval_res.get("selected_date"):
            fresh_hydrated = self.preprocessing._hydrate_rows(eval_res["selected_date"])
            fallback_map = {h["asin"]: h for h in fresh_hydrated}

        by_dir, by_asin = self._load_link_maps()
        standalone_candidates = self._compute_standalone_candidates(
            full_ranked, top_slice, by_asin, fallback_map
        )

        created_directions = [
            self.get_direction(did, observed_date=observed_date, preset_id=preset_id)
            for did in created_ids
        ]
        updated_directions = [
            self.get_direction(did, observed_date=observed_date, preset_id=preset_id)
            for did in updated_ids
        ]

        return {
            "dry_run": False,
            "top_n": safe_top_n,
            "selected_date": eval_res.get("selected_date"),
            "active_preset_id": eval_res.get("active_preset_id", "system_default"),
            "model": cluster_res.get("model"),
            "map_stats": cluster_res.get("map_stats") or {},
            "total_clusters_returned": len(cluster_res.get("clusters") or []),
            "rejected_clusters": cluster_res.get("rejected_clusters") or [],
            "created_count": len(created_directions),
            "updated_count": len(updated_directions),
            "skipped_existing": skipped_existing,
            "created_directions": created_directions,
            "updated_directions": updated_directions,
            "standalone_candidates": standalone_candidates,
            "standalone_count": len(standalone_candidates),
        }

    def generate_variants(
        self,
        direction_id: int,
        ai_service: Any,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """On-demand generation of 3~5 POD remix variant suggestions for a single direction."""
        direction = self.get_direction(
            int(direction_id),
            observed_date=observed_date,
            preset_id=preset_id,
        )
        res = ai_service.generate_direction_variants(direction, dry_run=dry_run)
        if dry_run:
            return res

        variants = normalize_variant_suggestions(res.get("variant_suggestions"))
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE print_directions
                       SET variant_suggestions = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        json.dumps(variants, ensure_ascii=False),
                        now,
                        int(direction_id),
                    ),
                )
                conn.commit()

        refreshed = self.get_direction(
            int(direction_id),
            observed_date=observed_date,
            preset_id=preset_id,
        )
        return {
            "dry_run": False,
            "direction_id": int(direction_id),
            "variant_suggestions": variants,
            "model": res.get("model"),
            "direction": refreshed,
        }

    # ------------------------------------------------------------------
    # Stage 06: Top 10 Output, Summary & Standardized Handoff Export
    # ------------------------------------------------------------------
    def _summarize_direction_confidence(
        self, supporting_products: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        counts = {"high": 0, "medium": 0, "low": 0}
        scores: List[float] = []
        for p in supporting_products:
            lvl = p.get("confidence_level")
            if lvl in counts:
                counts[lvl] += 1
            if p.get("confidence_score") is not None:
                scores.append(float(p["confidence_score"]))
        avg_conf = round(sum(scores) / len(scores), 1) if scores else None
        if avg_conf is None:
            overall_level, overall_label = "low", "无样本"
        elif avg_conf >= 80:
            overall_level, overall_label = "high", "高可信"
        elif avg_conf >= 60:
            overall_level, overall_label = "medium", "中可信"
        else:
            overall_level, overall_label = "low", "低可信"
        return {
            "high_count": counts["high"],
            "medium_count": counts["medium"],
            "low_count": counts["low"],
            "avg_confidence": avg_conf,
            "overall_level": overall_level,
            "overall_label": overall_label,
        }

    def get_top10(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return all `shortlisted` print directions sorted by `direction_score` DESC.

        Follows strict quality-over-quantity rule: if fewer than 10 directions are shortlisted,
        returns only the actual shortlisted count (never pads with drafts or fake items).
        """
        listed = self.list_directions(
            observed_date=observed_date,
            preset_id=preset_id,
            sort="direction_score",
            status="shortlisted",
        )
        raw_items = listed.get("items") or []
        raw_items.sort(
            key=lambda d: (
                -float(d.get("direction_score") or 0.0),
                -int((d.get("metrics") or {}).get("parent_asin_count") or 0),
                -int(d.get("id") or 0),
            )
        )

        # Cross-direction unique parents, ASINs, and category distribution
        all_unique_parents = set()
        all_unique_asins = set()
        cat_dist_map: Dict[str, Dict[str, Any]] = {}

        ranked_items: List[Dict[str, Any]] = []
        for idx, d in enumerate(raw_items, start=1):
            supporting = d.get("linked_products") or []
            conf_sum = self._summarize_direction_confidence(supporting)

            dir_cat_seen = set()
            for p in supporting:
                asin = p["asin"]
                all_unique_asins.add(asin)
                parent = (p.get("parent_asin") or "").strip().upper()
                all_unique_parents.add(parent if parent else asin)
                for src in p.get("source_categories") or []:
                    ckey = str(src.get("node_id") or src.get("category_key") or src.get("source_category") or "")
                    if not ckey:
                        continue
                    entry = cat_dist_map.setdefault(
                        ckey,
                        {
                            "node_id": src.get("node_id"),
                            "category_key": src.get("category_key"),
                            "name": src.get("source_category") or src.get("name") or ckey,
                            "short_name": src.get("short_name") or src.get("source_category") or ckey,
                            "color": src.get("color") or "#3B6BF6",
                            "direction_count": 0,
                            "_asins": set(),
                        },
                    )
                    entry["_asins"].add(asin)
                    if ckey not in dir_cat_seen:
                        dir_cat_seen.add(ckey)
                        entry["direction_count"] += 1

            ranked_items.append(
                {
                    **d,
                    "rank": idx,
                    "confidence_summary": conf_sum,
                    "supporting_products": supporting,
                }
            )

        category_distribution = []
        for entry in cat_dist_map.values():
            category_distribution.append(
                {
                    "node_id": entry["node_id"],
                    "category_key": entry["category_key"],
                    "name": entry["name"],
                    "short_name": entry["short_name"],
                    "color": entry["color"],
                    "direction_count": entry["direction_count"],
                    "asin_count": len(entry["_asins"]),
                }
            )
        category_distribution.sort(key=lambda c: (-c["direction_count"], -c["asin_count"], c["name"]))

        shortlisted_count = len(ranked_items)
        evergreen_items = [d for d in ranked_items if not d.get("is_seasonal")]
        seasonal_items = [d for d in ranked_items if d.get("is_seasonal")]
        return {
            "ready": bool(listed.get("ready")),
            "selected_date": listed.get("selected_date"),
            "active_preset_id": listed.get("active_preset_id", "system_default"),
            "active_scoring_config": listed.get("active_scoring_config"),
            "summary": {
                "shortlisted_count": shortlisted_count,
                "evergreen_count": len(evergreen_items),
                "seasonal_count": len(seasonal_items),
                "target_limit": 10,
                "exceeds_limit": shortlisted_count > 10,
                "total_unique_parents": len(all_unique_parents),
                "total_unique_asins": len(all_unique_asins),
                "covered_category_count": len(category_distribution),
                "category_distribution": category_distribution,
                "total_directions_all": (listed.get("status_counts") or {}).get("all", shortlisted_count),
            },
            "items": ranked_items,
            "evergreen_items": evergreen_items,
            "seasonal_items": seasonal_items,
        }

    def build_export_package(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build standardized JSON handoff package for downstream product development."""
        top10 = self.get_top10(observed_date=observed_date, preset_id=preset_id)
        exported_at = self._now_iso()
        directions_payload: List[Dict[str, Any]] = []

        for d in top10.get("items") or []:
            m = d.get("metrics") or {}
            supporting_out = [
                {
                    "asin": p["asin"],
                    "parent_asin": p.get("parent_asin"),
                    "title": p.get("title") or "",
                    "brand": p.get("brand") or "",
                    "main_image_url": p.get("main_image_url") or p.get("image_url") or "",
                    "amazon_url": p.get("amazon_url") or f"https://www.amazon.com/dp/{p['asin']}",
                    "sales_price_usd": p.get("sales_price_usd"),
                    "listing_sales_30d": p.get("listing_sales_30d"),
                    "price_anomaly": bool(p.get("price_anomaly")),
                    "sales_suspect": bool(p.get("sales_suspect")),
                    "covered_categories": p.get("covered_categories") or [],
                    "total_score": p.get("total_score"),
                    "confidence_level": p.get("confidence_level"),
                    "confidence_score": p.get("confidence_score"),
                    "season_tag": (p.get("ai_analysis") or {}).get("season_tag") or "",
                    "motif": (p.get("ai_analysis") or {}).get("motif") or "",
                    "transferability": (p.get("ai_analysis") or {}).get("transferability") or "",
                }
                for p in d.get("supporting_products") or []
            ]
            directions_payload.append(
                {
                    "rank": d["rank"],
                    "id": d["id"],
                    "name": d["name"],
                    "direction_type": d.get("direction_type") or "trend_cluster",
                    "direction_type_label": d.get("direction_type_label") or DIRECTION_TYPE_LABELS.get(d.get("direction_type") or "trend_cluster", "趋势集群"),
                    "source": d.get("source") or "manual",
                    "source_label": d.get("source_label") or DIRECTION_SOURCE_LABELS.get(d.get("source") or "manual", "人工创建"),
                    "style_tags": d.get("style_tags") or [],
                    "target_audience": d.get("target_audience") or "",
                    "feasibility_rating": d.get("feasibility_rating"),
                    "notes": d.get("notes") or "",
                    "status": d.get("status"),
                    "direction_score": d.get("direction_score"),
                    "all_anomalous": bool(m.get("all_anomalous")),
                    "excluded_anomaly_count": int(m.get("excluded_anomaly_count") or 0),
                    "anomalous_asins": list(m.get("anomalous_asins") or []),
                    "score_breakdown": m.get("score_breakdown") or {},
                    "parent_asin_count": m.get("parent_asin_count", 0),
                    "asin_count": m.get("asin_count", 0),
                    "brand_count": m.get("brand_count", 0),
                    "covered_categories": [
                        c.get("name") or c.get("short_name") or c.get("node_id")
                        for c in m.get("covered_categories") or []
                    ],
                    "max_sales_30d": m.get("max_sales_30d"),
                    "median_sales_30d": m.get("median_sales_30d"),
                    "price_range": m.get("price_range"),
                    "confidence_summary": d.get("confidence_summary") or {},
                    "supporting_products": supporting_out,
                    "season_tag": d.get("season_tag") or "evergreen",
                    "season_label": d.get("season_label") or SEASON_TAG_LABELS.get(d.get("season_tag") or "evergreen", "四季常青"),
                    "days_until_holiday": d.get("days_until_holiday"),
                    "variant_suggestions": list(d.get("variant_suggestions") or []),
                }
            )

        return {
            "export_version": "hotprint-top10-handoff-v1.0",
            "exported_at": exported_at,
            "data_snapshot_date": top10.get("selected_date"),
            "total_directions": len(directions_payload),
            "summary": top10.get("summary") or {},
            "directions": directions_payload,
        }

    def build_export_csv(
        self,
        *,
        observed_date: Optional[str] = None,
        preset_id: Optional[str] = None,
    ) -> Tuple[str, bytes]:
        """Build Excel-safe UTF-8 with BOM CSV (one row per shortlisted direction)."""
        pkg = self.build_export_package(observed_date=observed_date, preset_id=preset_id)
        snap_date = pkg.get("data_snapshot_date") or "latest"
        filename = f"hotprint-top10-directions-{snap_date}.csv"

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "排名",
                "方向ID",
                "方向主题名称",
                "方向综合分",
                "可行性星级",
                "风格标签",
                "目标受众",
                "独立父体数",
                "关联ASIN数",
                "独立品牌数",
                "覆盖类目",
                "最高30天估算销量",
                "中位30天估算销量",
                "有效价格带(USD)",
                "整体可信度",
                "设计交接备注",
                "代表性主图URL",
                "关联ASIN列表",
                "数据快照日期",
                "导出时间",
                "方向类型",
                "创建来源",
                "季节归属",
                "距节日天数",
                "二创变体建议",
            ]
        )

        for d in pkg["directions"]:
            pr = d.get("price_range")
            price_str = f"${pr['min']:.2f}-${pr['max']:.2f}" if pr else ""
            supporting = d.get("supporting_products") or []
            rep_img = next((p["main_image_url"] for p in supporting if p.get("main_image_url")), "")
            asins_str = ", ".join(p["asin"] for p in supporting)
            conf_sum = d.get("confidence_summary") or {}
            conf_str = (
                f"{conf_sum.get('overall_label', '')} ({conf_sum.get('avg_confidence')})"
                if conf_sum.get("avg_confidence") is not None
                else conf_sum.get("overall_label", "")
            )
            season_str = d.get("season_tag") or "evergreen"
            days_val = d.get("days_until_holiday")
            variants_str = " | ".join(d.get("variant_suggestions") or [])
            writer.writerow(
                [
                    d["rank"],
                    d["id"],
                    d["name"],
                    d["direction_score"],
                    d["feasibility_rating"],
                    ", ".join(d.get("style_tags") or []),
                    d.get("target_audience") or "",
                    d.get("parent_asin_count", 0),
                    d.get("asin_count", 0),
                    d.get("brand_count", 0),
                    ", ".join(d.get("covered_categories") or []),
                    d.get("max_sales_30d") if d.get("max_sales_30d") is not None else "",
                    d.get("median_sales_30d") if d.get("median_sales_30d") is not None else "",
                    price_str,
                    conf_str,
                    d.get("notes") or "",
                    rep_img,
                    asins_str,
                    pkg.get("data_snapshot_date") or "",
                    pkg.get("exported_at") or "",
                    d.get("direction_type_label") or d.get("direction_type") or "趋势集群",
                    d.get("source_label") or d.get("source") or "人工创建",
                    season_str,
                    days_val if days_val is not None else "",
                    variants_str,
                ]
            )

        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return filename, csv_bytes
