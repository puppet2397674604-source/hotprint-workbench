"""Stage 04: Candidate Product Scoring Engine (V1) & Preset Persistence.

Computes explainable dynamic-weight potential scores (0-100) across all six target categories
for preprocessed `ready` candidates, with:
- Strict cross-category ASIN deduplication (never sums sales across categories).
- Zero/missing sales isolated from ranked pool (`unranked_no_sales`).
- Five normalized dimensions:
  1. Sales Scale (log-compressed 0.6 * Global MinMax + 0.4 * Max Category Percentile)
  2. Opportunity / Sales-to-Reviews Ratio (log-compressed sales / (ratings_count + 20))
  3. Rating Quality (Bayesian smoothed m=30, mapped [3.5, 5.0] -> [0, 100])
  4. Freshness (sales-gated exponential decay 100 * exp(-d/180) * min(1, sales/50))
  5. Price Fit (garment-specific target intervals with 50% boundary linear decay)
- Missing-value re-normalization (missing dimensions excluded from numerator & denominator).
- Independent Data Confidence score (0-100) mapped to High / Medium / Low badges.
- SQLite persistence for user presets (`scoring_presets` table in app DB).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import (
    LOW_RATING_MILD_MULTIPLIER,
    LOW_RATING_MILD_THRESHOLD,
    LOW_RATING_SEVERE_MULTIPLIER,
    LOW_RATING_SEVERE_THRESHOLD,
    PRICE_ANOMALY_THRESHOLD_USD,
    SALES_BSR_MIN_SAMPLE_SIZE,
    SALES_BSR_RESIDUAL_SIGMA,
    resolve_category,
    serialize_categories,
)
from .preprocessing import (
    DEFAULT_APP_DB_PATH,
    PreprocessingService,
    detect_sales_bsr_anomalies,
    is_price_anomaly,
)
from .repository import extract_image_asset_key, normalize_text_exact

SCORING_VERSION = "scoring-v2.0"

MISSING_DIM_PRIOR = 45.0
LOW_RATING_MIN_REVIEWS = 10
BSR_OLS_CONSERVATIVE_FACTOR = 0.85
PRICE_ANOMALY_SCORE_MULTIPLIER = 0.85

DIMENSION_META: List[Dict[str, Any]] = [
    {
        "key": "sales",
        "label": "销量规模",
        "short_label": "销量",
        "default_weight": 35.0,
        "color": "#254EDB",
        "description": "P2–P98 缩尾对数体量 (60%) + 类目分位 (40%)；缺销量时按同类目 OLS 回归保守折算（×0.85）",
    },
    {
        "key": "opportunity",
        "label": "动销机会",
        "short_label": "机会",
        "default_weight": 25.0,
        "color": "#0F766E",
        "description": "近30天销量 / (评分数 + 20) 缩尾对数归一化，发掘少评高销款",
    },
    {
        "key": "rating",
        "label": "星级质量",
        "short_label": "星级",
        "default_weight": 15.0,
        "color": "#B45309",
        "description": "贝叶斯平滑 (m=30, C=合格池星级中位数)，[3.5, 5.0] 线性映射；差评降权需 ≥10 评",
    },
    {
        "key": "freshness",
        "label": "上架新鲜度",
        "short_label": "新鲜度",
        "default_weight": 15.0,
        "color": "#BE185D",
        "description": "指数衰减 100×exp(-d/180) × min(1, 销量/50)，防止零销新品虚高",
    },
    {
        "key": "price",
        "label": "价格适配度",
        "short_label": "价格",
        "default_weight": 10.0,
        "color": "#6D28D9",
        "description": "POD 非对称利润曲线：严惩低于底价（<$5 给 0 分且降权），宽容高毛利溢价款",
    },
]

DIMENSION_KEYS = tuple(d["key"] for d in DIMENSION_META)

DEFAULT_WEIGHTS: Dict[str, float] = {d["key"]: float(d["default_weight"]) for d in DIMENSION_META}

BUILTIN_STRATEGY_PRESETS: List[Dict[str, Any]] = [
    {
        "preset_id": "default",
        "name": "均衡默认",
        "is_default": True,
        "is_builtin": True,
        "weights": dict(DEFAULT_WEIGHTS),
    },
    {
        "preset_id": "evergreen",
        "name": "常青稳销",
        "is_default": False,
        "is_builtin": True,
        "weights": {"sales": 45.0, "opportunity": 15.0, "rating": 25.0, "freshness": 5.0, "price": 10.0},
    },
    {
        "preset_id": "rising_star",
        "name": "新品黑马",
        "is_default": False,
        "is_builtin": True,
        "weights": {"sales": 20.0, "opportunity": 35.0, "rating": 10.0, "freshness": 25.0, "price": 10.0},
    },
]

DEFAULT_PRICE_RANGES: Dict[str, Dict[str, float]] = {
    "t_shirt": {"min": 15.0, "max": 26.0},
    "sweatshirt": {"min": 26.0, "max": 45.0},
}

CONFIDENCE_LABELS: Dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

SCORING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scoring_presets (
    preset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    weights_json TEXT NOT NULL,
    price_ranges_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scoring_active_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    preset_id TEXT NOT NULL DEFAULT 'system_default',
    preset_name TEXT NOT NULL DEFAULT '系统默认方案',
    weights_json TEXT NOT NULL,
    price_ranges_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class ScoringValidationError(ValueError):
    """Raised when scoring weights, price ranges, or preset inputs are invalid."""


class ScoringNotFoundError(LookupError):
    """Raised when a scoring preset does not exist."""


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


def _parse_iso_date(raw: Optional[str]) -> Optional[date]:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()[:10]
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def validate_weights(raw: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Validate and coerce the 5 dimension weights (each 0..100)."""
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(raw, dict):
        raise ScoringValidationError("weights 必须为对象。")
    out: Dict[str, float] = {}
    for key in DIMENSION_KEYS:
        val = raw.get(key, DEFAULT_WEIGHTS[key])
        try:
            num = float(val)
        except (TypeError, ValueError) as exc:
            raise ScoringValidationError(f"权重 {key} 必须为数字。") from exc
        if math.isnan(num) or math.isinf(num) or num < 0 or num > 100:
            raise ScoringValidationError(f"权重 {key} 必须在 0 到 100 之间。")
        out[key] = round(num, 2)
    return out


def validate_price_ranges(raw: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Validate and coerce garment-specific target price intervals."""
    if not raw:
        return {k: dict(v) for k, v in DEFAULT_PRICE_RANGES.items()}
    if not isinstance(raw, dict):
        raise ScoringValidationError("price_ranges 必须为对象。")
    out: Dict[str, Dict[str, float]] = {}
    for garment, defaults in DEFAULT_PRICE_RANGES.items():
        cfg = raw.get(garment)
        if not cfg:
            out[garment] = dict(defaults)
            continue
        if not isinstance(cfg, dict):
            raise ScoringValidationError(f"品类价格配置 {garment} 必须为对象。")
        try:
            lo = float(cfg.get("min", defaults["min"]))
            hi = float(cfg.get("max", defaults["max"]))
        except (TypeError, ValueError) as exc:
            raise ScoringValidationError(f"品类 {garment} 价格上下限必须为数字。") from exc
        if math.isnan(lo) or math.isnan(hi) or lo <= 0 or hi <= 0 or lo >= hi or hi > 500:
            raise ScoringValidationError(f"品类 {garment} 目标价格区间无效（需满足 0 < min < max <= 500）。")
        out[garment] = {"min": round(lo, 2), "max": round(hi, 2)}
    return out


def _score_price_against_range(price_usd: float, p_min: float, p_max: float) -> float:
    """Asymmetric POD price fit curve against [p_min, p_max].

    - Inside [p_min, p_max]: 100.0
    - Below p_min (margin erosion): steep linear decay hitting 0 at 35% below p_min.
    - Above p_max (high-margin POD): gentle decay with a 40.0 floor so profitable designs aren't crushed.
    """
    if p_min <= price_usd <= p_max:
        return 100.0
    if price_usd < p_min:
        dev = (p_min - price_usd) / p_min
        return _clamp(100.0 * (1.0 - dev / 0.35), 0.0, 100.0)
    dev = (price_usd - p_max) / p_max
    return _clamp(max(40.0, 100.0 * (1.0 - dev * 0.6)), 0.0, 100.0)


def compute_confidence(
    item: Dict[str, Any],
    *,
    price_anomaly: Optional[bool] = None,
    sales_suspect: Optional[bool] = None,
) -> Dict[str, Any]:
    """Compute independent Data Confidence score (0..100) and penalty reasons."""
    score = 100
    penalties: List[Dict[str, Any]] = []

    rating_stars = item.get("rating_stars")
    ratings_count = item.get("ratings_count")
    if rating_stars is None or ratings_count is None:
        score -= 15
        penalties.append({"code": "rating_missing", "label": "缺星级或评分数", "delta": -15})

    online_dt = _parse_iso_date(item.get("online_date"))
    if online_dt is None:
        score -= 10
        penalties.append({"code": "online_date_missing", "label": "缺有效上架日期", "delta": -10})

    price = item.get("price")
    if price is None:
        price = item.get("sales_price_usd")
    if price is None:
        price = item.get("raw_price_usd")
    price_valid = item.get("price_valid")
    if price_valid is None:
        price_valid = price is not None and float(price) > 0
    if not price_valid or price is None or float(price) <= 0:
        score -= 15
        penalties.append({"code": "price_invalid", "label": "售价缺失或为 $0", "delta": -15})
    else:
        is_p_anom = (
            bool(price_anomaly)
            if price_anomaly is not None
            else (
                bool(item.get("price_anomaly"))
                or ("price_anomaly" in (item.get("data_quality_issues") or []))
                or is_price_anomaly(price)
            )
        )
        if is_p_anom:
            score -= 20
            penalties.append(
                {
                    "code": "price_anomaly",
                    "label": f"售价异常 (<${PRICE_ANOMALY_THRESHOLD_USD:g})",
                    "delta": -20,
                }
            )

    is_s_susp = (
        bool(sales_suspect)
        if sales_suspect is not None
        else (
            bool(item.get("sales_suspect"))
            or ("sales_suspect" in (item.get("data_quality_issues") or []))
        )
    )
    if is_s_susp:
        score -= 15
        penalties.append(
            {
                "code": "sales_suspect",
                "label": f"销量与类目 BSR 严重背离 (>{SALES_BSR_RESIDUAL_SIGMA:g}σ)",
                "delta": -15,
            }
        )

    asin = str(item.get("asin") or "").strip().upper()
    parent_asin = str(item.get("parent_asin") or "").strip().upper()
    if asin and parent_asin and asin == parent_asin:
        score -= 15
        penalties.append({"code": "parent_asin_same", "label": "父子 ASIN 相同待核验", "delta": -15})

    if ratings_count is not None and int(ratings_count) < 10:
        score -= 10
        penalties.append({"code": "thin_reviews", "label": "评价样本偏薄 (<10)", "delta": -10})

    score = max(0, min(100, int(score)))
    if score >= 80:
        level = "high"
    elif score >= 60:
        level = "medium"
    else:
        level = "low"

    return {
        "score": score,
        "level": level,
        "label": CONFIDENCE_LABELS[level],
        "penalties": penalties,
    }


def _deduplicate_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strict Tier-1 ASIN + Tier-2 Listing-level deduplication without ever summing sales.

    First deduplicates identical ASINs across categories; then collapses sibling variant/orphan
    listings that share:
      1. The same valid parent_asin (`parent_asin` present and `parent_asin != asin`), OR
      2. The same non-empty brand + identical Amazon image asset key (`image_asset_key`), OR
      3. The same non-empty brand + exact identical normalized title (len >= 20).
    For each collapsed cluster, selects the single highest-sales representative listing (never
    summing `listing_sales_30d`), merges `source_categories`, and records `collapsed_sibling_asins`.
    """
    # Tier 1: exact ASIN dedup
    by_asin: Dict[str, Dict[str, Any]] = {}
    for raw in candidates:
        asin = str(raw.get("asin") or "").strip().upper()
        if not asin:
            continue
        if asin not in by_asin:
            copied = dict(raw)
            copied["asin"] = asin
            copied["source_categories"] = list(raw.get("source_categories") or [])
            copied["garment_types"] = list(raw.get("garment_types") or [])
            copied["genders"] = list(raw.get("genders") or [])
            by_asin[asin] = copied
        else:
            existing = by_asin[asin]
            seen_nodes = {
                s.get("node_id") or s.get("category_key")
                for s in existing["source_categories"]
            }
            for src in raw.get("source_categories") or []:
                key = src.get("node_id") or src.get("category_key")
                if key not in seen_nodes:
                    seen_nodes.add(key)
                    existing["source_categories"].append(src)
            for gt in raw.get("garment_types") or []:
                if gt not in existing["garment_types"]:
                    existing["garment_types"].append(gt)
            for gd in raw.get("genders") or []:
                if gd not in existing["genders"]:
                    existing["genders"].append(gd)
            existing["is_cross_category"] = len(existing["source_categories"]) > 1
            existing["cross_category_count"] = len(existing["source_categories"])

    items = list(by_asin.values())
    n = len(items)
    if n <= 1:
        for it in items:
            it.setdefault("collapsed_sibling_asins", [])
            it.setdefault("collapsed_count", 0)
        return items

    # Tier 2: Union-Find listing collapse (same valid parent, same brand+image, or same brand+exact title)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    parent_idx: Dict[str, int] = {}
    brand_img_idx: Dict[Tuple[str, str], int] = {}
    brand_title_idx: Dict[Tuple[str, str], int] = {}

    for idx, it in enumerate(items):
        asin = it["asin"]
        pasin = str(it.get("parent_asin") or "").strip().upper()
        if pasin and pasin != asin:
            if pasin in parent_idx:
                union(parent_idx[pasin], idx)
            else:
                parent_idx[pasin] = idx

        brand_norm = normalize_text_exact(it.get("brand"))
        if brand_norm and brand_norm not in ("unknown", "generic", "n/a"):
            img_key = extract_image_asset_key(it.get("image_url") or it.get("main_image_url"))
            if img_key:
                k_img = (brand_norm, img_key)
                if k_img in brand_img_idx:
                    union(brand_img_idx[k_img], idx)
                else:
                    brand_img_idx[k_img] = idx

            title_norm = normalize_text_exact(it.get("title"))
            if len(title_norm) >= 20:
                k_title = (brand_norm, title_norm)
                if k_title in brand_title_idx:
                    union(brand_title_idx[k_title], idx)
                else:
                    brand_title_idx[k_title] = idx

    clusters: Dict[int, List[Dict[str, Any]]] = {}
    for idx, it in enumerate(items):
        clusters.setdefault(find(idx), []).append(it)

    deduped: List[Dict[str, Any]] = []
    for members in clusters.values():
        if len(members) == 1:
            one = members[0]
            one["collapsed_sibling_asins"] = []
            one["collapsed_count"] = 0
            deduped.append(one)
            continue

        # Select representative: highest valid sales (prefer valid price & valid rating on ties)
        def rep_key(m: Dict[str, Any]) -> Tuple[Any, ...]:
            s30 = m.get("listing_sales_30d")
            has_sales = 1 if (s30 is not None and float(s30) > 0) else 0
            sales_val = float(s30) if s30 is not None else -1.0
            has_price = 1 if (m.get("price_valid") and m.get("price") is not None and float(m.get("price")) > 0) else 0
            has_rating = 1 if m.get("rating_stars") is not None else 0
            has_parent = 1 if (m.get("parent_asin") and m.get("parent_asin") != m.get("asin")) else 0
            return (has_sales, sales_val, has_price, has_rating, has_parent, m["asin"])

        members_sorted = sorted(members, key=rep_key, reverse=True)
        rep = dict(members_sorted[0])
        rep["source_categories"] = list(rep.get("source_categories") or [])
        rep["garment_types"] = list(rep.get("garment_types") or [])
        rep["genders"] = list(rep.get("genders") or [])

        seen_nodes = {
            s.get("node_id") or s.get("category_key")
            for s in rep["source_categories"]
        }
        siblings: List[str] = []
        for other in members_sorted[1:]:
            siblings.append(other["asin"])
            for src in other.get("source_categories") or []:
                key = src.get("node_id") or src.get("category_key")
                if key not in seen_nodes:
                    seen_nodes.add(key)
                    rep["source_categories"].append(src)
            for gt in other.get("garment_types") or []:
                if gt not in rep["garment_types"]:
                    rep["garment_types"].append(gt)
            for gd in other.get("genders") or []:
                if gd not in rep["genders"]:
                    rep["genders"].append(gd)

        rep["is_cross_category"] = len(rep["source_categories"]) > 1
        rep["cross_category_count"] = len(rep["source_categories"])
        rep["collapsed_sibling_asins"] = siblings
        rep["collapsed_count"] = len(siblings)
        deduped.append(rep)

    return deduped


def _extract_node_bsrs(item: Dict[str, Any]) -> Dict[str, Tuple[int, str]]:
    """Extract valid positive `source_category_bsr` per category node_id/key for a candidate."""
    out: Dict[str, Tuple[int, str]] = {}
    bsr_map = item.get("category_bsr_by_source")
    if not isinstance(bsr_map, dict):
        bsr_map = {}

    for src in item.get("source_categories") or []:
        if not isinstance(src, dict):
            continue
        ckey = str(src.get("node_id") or src.get("category_key") or "").strip()
        if not ckey:
            continue
        raw_bsr = src.get("source_category_bsr")
        if raw_bsr is None and ckey in bsr_map:
            entry = bsr_map[ckey]
            raw_bsr = entry.get("bsr") if isinstance(entry, dict) else entry
        if raw_bsr is None:
            continue
        try:
            bsr_val = float(raw_bsr)
        except (TypeError, ValueError):
            continue
        if math.isnan(bsr_val) or math.isinf(bsr_val) or bsr_val <= 0:
            continue
        bsr_int = int(round(bsr_val))
        if bsr_int <= 0:
            continue
        cname = str(src.get("short_name") or src.get("source_category") or src.get("name") or ckey)
        if ckey not in out or bsr_int < out[ckey][0]:
            out[ckey] = (bsr_int, cname)

    if not out:
        for ckey, entry in bsr_map.items():
            raw_bsr = entry.get("bsr") if isinstance(entry, dict) else entry
            if raw_bsr is None:
                continue
            try:
                bsr_int = int(round(float(raw_bsr)))
            except (TypeError, ValueError):
                continue
            if bsr_int > 0:
                cname = (
                    str(entry.get("source_category") or ckey)
                    if isinstance(entry, dict)
                    else str(ckey)
                )
                out[str(ckey)] = (bsr_int, cname)

    if not out and item.get("source_category_bsr") is not None:
        try:
            bsr_int = int(round(float(item["source_category_bsr"])))
            if bsr_int > 0:
                out["default"] = (bsr_int, "当前类目")
        except (TypeError, ValueError):
            pass

    return out


def _bsr_reverse_percentile_in_group(bsr_value: float, group_bsrs: List[float]) -> float:
    """Compute within-category BSR reverse percentile in [0, 100].

    Smaller BSR rank number = higher score:
      score = 100 * (1 - (rank_pos - 1) / max(1, N - 1))
    Single-sample category (N <= 1) returns 50.0.
    """
    n = len(group_bsrs)
    if n <= 1:
        return 50.0
    less = sum(1 for v in group_bsrs if v < bsr_value)
    equal = sum(1 for v in group_bsrs if v == bsr_value)
    rank_pos = 1.0 + less + 0.5 * max(0, equal - 1)
    return _clamp(100.0 * (1.0 - (rank_pos - 1.0) / max(1, n - 1)), 0.0, 100.0)


def _percentile_in_group(value: float, group_values: List[float]) -> float:
    """Compute tie-aware percentile rank in [0, 100] within a category group."""
    n = len(group_values)
    if n <= 1:
        return 100.0
    less = sum(1 for v in group_values if v < value)
    equal = sum(1 for v in group_values if v == value)
    return _clamp(100.0 * (less + 0.5 * max(0, equal - 1)) / (n - 1), 0.0, 100.0)


def _winsorized_bounds(values: List[float], low_pct: float = 0.02, high_pct: float = 0.98) -> Tuple[float, float]:
    """Compute P2–P98 winsorized bounds to prevent single outliers from crushing 95% of items."""
    if not values:
        return 0.0, 0.0
    s = sorted(values)
    n = len(s)
    if n < 10:
        return s[0], s[-1]
    lo_idx = max(0, min(n - 1, int(math.floor((n - 1) * low_pct))))
    hi_idx = max(0, min(n - 1, int(math.ceil((n - 1) * high_pct))))
    if s[hi_idx] <= s[lo_idx]:
        return s[0], s[-1]
    return s[lo_idx], s[hi_idx]


def _minmax_norm(value: float, min_val: float, max_val: float, count: int) -> float:
    if count <= 1:
        return 100.0
    if max_val <= min_val:
        return 50.0
    return _clamp(100.0 * (value - min_val) / (max_val - min_val), 0.0, 100.0)


def _weighted_total_and_breakdown(
    dim_scores: Dict[str, Optional[float]],
    dim_meta_extra: Dict[str, Dict[str, Any]],
    weights: Dict[str, float],
) -> Tuple[float, Dict[str, Dict[str, Any]], List[str]]:
    """Compute S = sum(w_i * eff_s_i) / sum(w_i), assigning conservative prior (45.0) to missing dims.

    Eliminates the missing-data free-rider loophole where omitting rating/opportunity boosted total score.
    """
    missing_keys = [k for k in DIMENSION_KEYS if dim_scores.get(k) is None]
    total_weight_sum = sum(weights[k] for k in DIMENSION_KEYS)

    total = 0.0
    breakdown: Dict[str, Dict[str, Any]] = {}
    for meta in DIMENSION_META:
        k = meta["key"]
        raw_score = dim_scores.get(k)
        w = weights[k]
        extra = dim_meta_extra.get(k, {})
        eff_w = (w / total_weight_sum) if total_weight_sum > 0 else 0.0
        if raw_score is None:
            prior_contrib = MISSING_DIM_PRIOR * eff_w
            total += prior_contrib
            breakdown[k] = {
                "key": k,
                "label": meta["label"],
                "short_label": meta["short_label"],
                "color": meta["color"],
                "score": None,
                "prior_score": MISSING_DIM_PRIOR,
                "weight": round(w, 2),
                "effective_weight": round(eff_w * 100.0, 1),
                "contribution": round(prior_contrib, 2),
                "missing": True,
                **extra,
            }
        else:
            contrib = raw_score * eff_w
            total += contrib
            breakdown[k] = {
                "key": k,
                "label": meta["label"],
                "short_label": meta["short_label"],
                "color": meta["color"],
                "score": round(raw_score, 2),
                "weight": round(w, 2),
                "effective_weight": round(eff_w * 100.0, 1),
                "contribution": round(contrib, 2),
                "missing": False,
                **extra,
            }
    return round(total, 2), breakdown, missing_keys


def compute_low_rating_penalty(
    rating_stars: Any,
    ratings_count: Any,
) -> Tuple[Optional[str], float]:
    """Return `(penalty_level, score_multiplier)` for Stage 04 low-rating demotion.

    Requires minimum sample size (`ratings_count >= 10`) so 1-2 early size reviews never halve a new listing:
    - `rating_stars < 3.0` -> `("severe", 0.55)`
    - `3.0 <= rating_stars < 3.8` -> `("mild", 0.85)`
    - `rating_stars >= 3.8` or missing/< 10 reviews -> `(None, 1.0)`
    """
    if rating_stars is None or ratings_count is None:
        return None, 1.0
    try:
        stars_f = float(rating_stars)
        rc_i = int(round(float(ratings_count)))
    except (TypeError, ValueError):
        return None, 1.0
    if math.isnan(stars_f) or stars_f <= 0 or rc_i < LOW_RATING_MIN_REVIEWS:
        return None, 1.0
    if stars_f < LOW_RATING_SEVERE_THRESHOLD:
        return "severe", float(LOW_RATING_SEVERE_MULTIPLIER)
    if stars_f < LOW_RATING_MILD_THRESHOLD:
        return "mild", float(LOW_RATING_MILD_MULTIPLIER)
    return None, 1.0


def compute_candidate_scores(
    candidates: List[Dict[str, Any]],
    *,
    observed_date: Optional[str] = None,
    weights: Optional[Dict[str, Any]] = None,
    price_ranges: Optional[Dict[str, Any]] = None,
    previous_ranks: Optional[Dict[str, int]] = None,
    bsr_reference_pool: Optional[List[Dict[str, Any]]] = None,
    filter_incomplete: bool = True,
) -> Dict[str, Any]:
    """Pure, deterministic V1 scoring engine over a pool of ready candidates.

    - Deduplicates by ASIN defensively (never doubles sales).
    - Separates `listing_sales_30d <= 0` or `None` into `unranked_no_sales`.
    - Flags `price_anomaly` (0 < price < $5) and per-category `sales_suspect` (OLS residual > 2σ).
    - Computes both active-weight ranking and default-weight baseline ranking for rank deltas.
    """
    eff_weights = validate_weights(weights)
    eff_prices = validate_price_ranges(price_ranges)
    deduped = _deduplicate_candidates(candidates)

    bsr_audit = detect_sales_bsr_anomalies(
        bsr_reference_pool if bsr_reference_pool is not None else deduped,
        sigma_threshold=SALES_BSR_RESIDUAL_SIGMA,
        min_sample_size=SALES_BSR_MIN_SAMPLE_SIZE,
    )
    suspect_map = bsr_audit["suspect_map"]

    obs_dt = _parse_iso_date(observed_date) or date.today()

    valid_pool: List[Dict[str, Any]] = []
    unranked_no_sales: List[Dict[str, Any]] = []
    node_bsrs_by_asin: Dict[str, Dict[str, Tuple[int, str]]] = {}

    for item in deduped:
        asin = item["asin"]
        sales = item.get("listing_sales_30d")
        has_valid_sales = False
        if sales is not None:
            try:
                has_valid_sales = float(sales) > 0 and not math.isnan(float(sales))
            except (TypeError, ValueError):
                has_valid_sales = False

        node_bsrs = _extract_node_bsrs(item)
        node_bsrs_by_asin[asin] = node_bsrs
        has_valid_bsr = len(node_bsrs) > 0

        if not has_valid_sales and not has_valid_bsr:
            raw_p = item.get("price")
            if raw_p is None:
                raw_p = item.get("sales_price_usd")
            p_anom = (
                bool(item.get("price_anomaly"))
                or ("price_anomaly" in (item.get("data_quality_issues") or []))
                or is_price_anomaly(raw_p)
            )
            conf = compute_confidence(item, price_anomaly=p_anom, sales_suspect=False)
            unranked_no_sales.append(
                {
                    "asin": asin,
                    "parent_asin": item.get("parent_asin"),
                    "title": item.get("title") or "",
                    "brand": item.get("brand") or "",
                    "image_url": item.get("image_url") or item.get("main_image_url") or "",
                    "source_categories": item.get("source_categories") or [],
                    "garment_types": item.get("garment_types") or [],
                    "listing_sales_30d": sales,
                    "price": item.get("price") if item.get("price_valid", True) else None,
                    "price_anomaly": p_anom,
                    "sales_suspect": False,
                    "sales_suspect_details": [],
                    "rating_stars": item.get("rating_stars"),
                    "ratings_count": item.get("ratings_count"),
                    "online_date": item.get("online_date"),
                    "confidence_score": conf["score"],
                    "confidence_level": conf["level"],
                    "confidence_label": conf["label"],
                    "confidence_penalties": conf["penalties"],
                    "reason": "近 30 天估算销量与源类目 BSR 均缺失或为 0，暂不参与潜力评分排名",
                }
            )
        else:
            valid_pool.append(item)

    if not valid_pool:
        return {
            "scoring_version": SCORING_VERSION,
            "observed_date": obs_dt.isoformat(),
            "weights": eff_weights,
            "price_ranges": eff_prices,
            "default_weights": dict(DEFAULT_WEIGHTS),
            "default_price_ranges": {k: dict(v) for k, v in DEFAULT_PRICE_RANGES.items()},
            "dimensions_meta": DIMENSION_META,
            "rating_median_c": 4.3,
            "sales_bsr_audit": bsr_audit["categories"],
            "summary": {
                "scored_count": 0,
                "unranked_no_sales_count": len(unranked_no_sales),
                "unranked_incomplete_count": 0,
                "avg_score": None,
                "median_score": None,
                "confidence_counts": {"high": 0, "medium": 0, "low": 0},
                "price_anomaly_count": 0,
                "sales_suspect_count": 0,
            },
            "items": [],
            "unranked_no_sales": unranked_no_sales,
            "unranked_incomplete": [],
        }

    # --- Precompute pool-wide statistics over valid_pool (V2 unified scale with OLS alignment) ---
    bsr_cat_models = bsr_audit.get("categories") or {}
    effective_sales_by_asin: Dict[str, Tuple[float, str, Optional[str]]] = {}
    log_sales_by_asin: Dict[str, float] = {}
    cat_log_sales: Dict[str, List[float]] = {}
    cat_bsr_values: Dict[str, List[float]] = {}

    for item in valid_pool:
        asin = item["asin"]
        node_bsrs = node_bsrs_by_asin.get(asin, {})
        for ckey, (bsr_int, _) in node_bsrs.items():
            cat_bsr_values.setdefault(ckey, []).append(float(bsr_int))

        raw_sales = item.get("listing_sales_30d")
        if raw_sales is not None and float(raw_sales) > 0:
            eff_sales = float(raw_sales)
            cap_note: Optional[str] = None
            # Cap sales_suspect positive outliers at 1.5x OLS BSR-predicted sales
            s_det_list = suspect_map.get(asin) or item.get("sales_suspect_details") or []
            high_preds = [
                float(d["predicted_sales_30d"])
                for d in s_det_list
                if isinstance(d, dict)
                and d.get("direction") == "high_sales_vs_bsr"
                and d.get("predicted_sales_30d")
                and float(d["predicted_sales_30d"]) > 0
            ]
            if high_preds:
                cap_val = max(high_preds) * 1.5
                if eff_sales > cap_val:
                    cap_note = f"销量存疑已按 BSR 预测封顶（原 {int(round(eff_sales)):,} → {int(round(cap_val)):,}）"
                    eff_sales = cap_val
            effective_sales_by_asin[asin] = (eff_sales, "listing_sales_30d", cap_note)
        else:
            # Try conservative OLS BSR->Sales alignment (x0.85) on evaluated categories
            best_est: Optional[Tuple[float, int, str]] = None
            for ckey, (bsr_int, cname) in node_bsrs.items():
                mod = bsr_cat_models.get(ckey)
                if (
                    isinstance(mod, dict)
                    and mod.get("status") == "evaluated"
                    and mod.get("slope") is not None
                    and mod.get("intercept") is not None
                    and float(mod["slope"]) < 0
                ):
                    pred_y = float(mod["intercept"]) + float(mod["slope"]) * math.log(float(bsr_int))
                    if -5.0 < pred_y < 14.0:
                        est_val = max(1.0, math.exp(pred_y) * BSR_OLS_CONSERVATIVE_FACTOR)
                        if best_est is None or est_val > best_est[0]:
                            best_est = (est_val, bsr_int, cname)
            if best_est is not None:
                est_val, bsr_int, cname = best_est
                note = f"{cname} BSR #{bsr_int:,} 经同类目 OLS 保守折算（×{BSR_OLS_CONSERVATIVE_FACTOR:g}）≈ {int(round(est_val)):,} 件/月"
                effective_sales_by_asin[asin] = (est_val, "bsr_ols_estimated", note)

        if asin in effective_sales_by_asin:
            lx = math.log1p(effective_sales_by_asin[asin][0])
            log_sales_by_asin[asin] = lx
            srcs = item.get("source_categories") or []
            if srcs:
                seen_c = set()
                for s in srcs:
                    ckey = str(s.get("node_id") or s.get("category_key") or "default")
                    if ckey not in seen_c:
                        seen_c.add(ckey)
                        cat_log_sales.setdefault(ckey, []).append(lx)
            else:
                cat_log_sales.setdefault("default", []).append(lx)

    all_log_sales = list(log_sales_by_asin.values())
    sales_x_min, sales_x_max = _winsorized_bounds(all_log_sales, 0.02, 0.98)

    # Precompute Dim 1 (s_sales / demand score) for every item in valid_pool
    demand_info_by_asin: Dict[str, Dict[str, Any]] = {}
    for item in valid_pool:
        asin = item["asin"]
        srcs = item.get("source_categories") or []
        node_bsrs = node_bsrs_by_asin.get(asin, {})
        best_bsr_val = min((v[0] for v in node_bsrs.values()), default=None)

        if asin in log_sales_by_asin:
            sales_val, src_kind, extra_note = effective_sales_by_asin[asin]
            lx = log_sales_by_asin[asin]
            global_mm = _minmax_norm(lx, sales_x_min, sales_x_max, len(all_log_sales))
            if srcs:
                cat_pcts = [
                    _percentile_in_group(
                        lx,
                        cat_log_sales.get(str(s.get("node_id") or s.get("category_key") or "default"), [lx]),
                    )
                    for s in srcs
                ]
                cat_pct = max(cat_pcts)
            else:
                cat_pct = _percentile_in_group(lx, cat_log_sales.get("default", [lx]))
            s_sales = _clamp(0.6 * global_mm + 0.4 * cat_pct, 0.0, 100.0)
            if src_kind == "bsr_ols_estimated":
                raw_disp = f"BSR #{best_bsr_val:,} · 折算 ~{int(round(sales_val)):,}/月" if best_bsr_val else f"折算 ~{int(round(sales_val)):,}/月"
                det_text = f"{extra_note} · 统一标尺 {s_sales:.1f}"
            else:
                raw_disp = f"{int(round(float(item['listing_sales_30d']))):,} 件/月"
                det_text = f"缩尾对数 {global_mm:.1f} · 类目分位 {cat_pct:.1f}" + (f" · {extra_note}" if extra_note else "")
            demand_info_by_asin[asin] = {
                "source": src_kind,
                "s_sales": s_sales,
                "sales_val": sales_val,
                "best_bsr": best_bsr_val,
                "raw_display": raw_disp,
                "detail": det_text,
            }
        else:
            best_rev_pct = -1.0
            best_cat_name = "当前类目"
            best_cat_bsr = best_bsr_val or 0
            best_cat_n = 1
            for ckey, (bsr_int, cname) in node_bsrs.items():
                group_list = cat_bsr_values.get(ckey, [float(bsr_int)])
                rev_pct = _bsr_reverse_percentile_in_group(float(bsr_int), group_list)
                if rev_pct > best_rev_pct or (rev_pct == best_rev_pct and bsr_int < best_cat_bsr):
                    best_rev_pct = rev_pct
                    best_cat_name = cname
                    best_cat_bsr = bsr_int
                    best_cat_n = len(group_list)
            s_sales = _clamp(best_rev_pct if best_rev_pct >= 0 else 50.0, 0.0, 100.0)
            demand_info_by_asin[asin] = {
                "source": "bsr_reverse_percentile",
                "s_sales": s_sales,
                "sales_val": None,
                "best_bsr": best_cat_bsr if best_cat_bsr > 0 else None,
                "raw_display": f"BSR #{best_cat_bsr:,} (超越同类 {s_sales:.1f}%)" if best_cat_bsr > 0 else "超越同类占比",
                "detail": (
                    f"基于榜单排名，超越同类目 {s_sales:.1f}% 的商品"
                    f"（{best_cat_name} BSR #{best_cat_bsr:,}，同节点 n={best_cat_n}）"
                ),
            }

    # 2A. Opportunity log values for unified sales-scale items (Track A + OLS-aligned Track B)
    opp_log_by_asin: Dict[str, float] = {}
    # 2B. Fallback opportunity proxy for small-category BSR-only items
    bsr_opp_raw_by_asin: Dict[str, float] = {}

    for item in valid_pool:
        asin = item["asin"]
        rc = item.get("ratings_count")
        if rc is None:
            continue
        dinfo = demand_info_by_asin[asin]
        if dinfo["sales_val"] is not None:
            r_ratio = float(dinfo["sales_val"]) / (max(0.0, float(rc)) + 20.0)
            opp_log_by_asin[asin] = math.log1p(r_ratio)
        else:
            bsr_opp_raw_by_asin[asin] = float(dinfo["s_sales"]) / math.log(math.e + max(0.0, float(rc)))

    opp_vals = list(opp_log_by_asin.values())
    opp_min, opp_max = _winsorized_bounds(opp_vals, 0.02, 0.98)

    bsr_opp_vals = list(bsr_opp_raw_by_asin.values())
    bsr_opp_min, bsr_opp_max = _winsorized_bounds(bsr_opp_vals, 0.02, 0.98)

    # 3. Rating median C across valid ratings in qualified pool (fallback 4.3)
    valid_stars = [
        float(item["rating_stars"])
        for item in valid_pool
        if item.get("rating_stars") is not None and item.get("ratings_count") is not None
    ]
    rating_c = _median(valid_stars)
    if rating_c is None:
        rating_c = 4.3
    rating_m = 30.0

    # --- Compute per-item 5 dimension scores (once per item) ---
    dim_short_by_key = {d["key"]: d["short_label"] for d in DIMENSION_META}
    enriched: List[Dict[str, Any]] = []
    unranked_incomplete: List[Dict[str, Any]] = []
    for item in valid_pool:
        asin = item["asin"]
        srcs = item.get("source_categories") or []
        dinfo = demand_info_by_asin[asin]
        demand_source = dinfo["source"]
        sales_val = dinfo["sales_val"]
        s_sales = dinfo["s_sales"]

        # Dim 2: Opportunity (unified scale for Track A + OLS-aligned Track B)
        rc = item.get("ratings_count")
        if rc is None:
            s_opp: Optional[float] = None
            opp_ratio: Optional[float] = None
            opp_raw_display = "缺失"
            opp_detail = f"缺少评分数，按保守先验 {MISSING_DIM_PRIOR:g} 分计入"
        elif sales_val is not None:
            opp_ratio = float(sales_val) / (max(0.0, float(rc)) + 20.0)
            s_opp = _minmax_norm(opp_log_by_asin[asin], opp_min, opp_max, len(opp_vals))
            opp_raw_display = f"{opp_ratio:.1f}"
            opp_detail = f"动销比 r={opp_ratio:.2f} (评分数 {rc})"
        else:
            opp_ratio = bsr_opp_raw_by_asin[asin]
            if len(bsr_opp_vals) >= 2 and bsr_opp_max > bsr_opp_min:
                s_opp = _minmax_norm(opp_ratio, bsr_opp_min, bsr_opp_max, len(bsr_opp_vals))
            else:
                s_opp = _clamp(opp_ratio, 0.0, 100.0)
            opp_raw_display = f"{opp_ratio:.1f} (BSR机会)"
            opp_detail = f"BSR机会比 s_sales/ln(e+{rc})={opp_ratio:.2f}"

        # Dim 3: Bayesian Rating Quality
        stars = item.get("rating_stars")
        if stars is None or rc is None:
            s_rating: Optional[float] = None
            r_bayes: Optional[float] = None
        else:
            v = max(0.0, float(rc))
            r_val = float(stars)
            r_bayes = (v * r_val + rating_m * rating_c) / (v + rating_m)
            s_rating = _clamp(((r_bayes - 3.5) / 1.5) * 100.0, 0.0, 100.0)

        # Dim 4: Demand-Gated Freshness
        online_dt = _parse_iso_date(item.get("online_date"))
        if online_dt is None:
            s_fresh: Optional[float] = None
            days_online: Optional[int] = None
            fresh_detail = f"缺少上架日期，按保守先验 {MISSING_DIM_PRIOR:g} 分计入"
        else:
            days_online = max(0, (obs_dt - online_dt).days)
            base_f = 100.0 * math.exp(-days_online / 180.0)
            if sales_val is not None:
                demand_gate = min(1.0, float(sales_val) / 50.0)
                gate_label = f"销量门控 {demand_gate * 100:.0f}%"
            else:
                demand_gate = min(1.0, float(s_sales) / 50.0)
                gate_label = f"超越同类占比门控 {demand_gate * 100:.0f}%"
            s_fresh = _clamp(base_f * demand_gate, 0.0, 100.0)
            fresh_detail = f"上架 {days_online} 天 · {gate_label}"

        # Dim 5: Asymmetric POD Price Fit (price_anomaly <$5 scores 0.0 + profit penalty, never free-rides!)
        raw_price = item.get("price")
        if raw_price is None:
            raw_price = item.get("sales_price_usd")
        if raw_price is None:
            raw_price = item.get("raw_price_usd")
        price_valid = item.get("price_valid")
        if price_valid is None:
            price_valid = raw_price is not None and float(raw_price) > 0
        p_anom = bool(
            price_valid
            and raw_price is not None
            and float(raw_price) > 0
            and (
                bool(item.get("price_anomaly"))
                or ("price_anomaly" in (item.get("data_quality_issues") or []))
                or is_price_anomaly(raw_price)
            )
        )
        s_details = list(suspect_map.get(asin) or item.get("sales_suspect_details") or [])
        s_susp = bool(s_details) or bool(item.get("sales_suspect"))

        garments = list(item.get("garment_types") or [])
        if not garments:
            for s in srcs:
                gt = s.get("garment_type")
                if gt and gt not in garments:
                    garments.append(gt)
        if not garments:
            garments = ["t_shirt"]

        if not price_valid or raw_price is None or float(raw_price) <= 0:
            s_price: Optional[float] = None
            matched_range: Optional[Dict[str, float]] = None
            matched_garment = garments[0]
        elif p_anom:
            s_price = 0.0
            matched_range = None
            matched_garment = garments[0]
        else:
            p_val = float(raw_price)
            best_score = -1.0
            best_range = eff_prices.get("t_shirt", DEFAULT_PRICE_RANGES["t_shirt"])
            best_garment = garments[0]
            for g in garments:
                rng = eff_prices.get(g) or eff_prices["t_shirt"]
                sc = _score_price_against_range(p_val, rng["min"], rng["max"])
                if sc > best_score:
                    best_score = sc
                    best_range = rng
                    best_garment = g
            s_price = best_score
            matched_range = best_range
            matched_garment = best_garment

        dim_scores = {
            "sales": s_sales,
            "opportunity": s_opp,
            "rating": s_rating,
            "freshness": s_fresh,
            "price": s_price,
        }
        dim_extra = {
            "sales": {
                "raw_display": dinfo["raw_display"],
                "detail": dinfo["detail"],
                "demand_metric_source": demand_source,
            },
            "opportunity": {
                "raw_display": opp_raw_display,
                "detail": opp_detail,
            },
            "rating": {
                "raw_display": f"{float(stars):.1f}★ ({int(rc):,})" if stars is not None and rc is not None else "缺失",
                "detail": f"贝叶斯平滑 {r_bayes:.2f}★ (基准 {rating_c:.2f}★)" if r_bayes is not None else f"缺少星级或评分数，按保守先验 {MISSING_DIM_PRIOR:g} 分计入",
            },
            "freshness": {
                "raw_display": f"{days_online} 天" if days_online is not None else "缺失",
                "detail": fresh_detail,
            },
            "price": {
                "raw_display": (
                    f"${float(raw_price):.2f} (异常低价)"
                    if p_anom and raw_price is not None
                    else (f"${float(raw_price):.2f}" if s_price is not None and raw_price is not None else "缺失/$0")
                ),
                "detail": (
                    f"售价低于 ${PRICE_ANOMALY_THRESHOLD_USD:g} 利润警戒线，价格分记 0 并触发 ×{PRICE_ANOMALY_SCORE_MULTIPLIER:g} 降权"
                    if p_anom
                    else (
                        f"匹配{'卫衣' if matched_garment == 'sweatshirt' else 'T恤'}目标带 ${matched_range['min']:.0f}–${matched_range['max']:.0f}（非对称利润曲线）"
                        if matched_range is not None
                        else f"售价缺失或为 $0，按保守先验 {MISSING_DIM_PRIOR:g} 分计入"
                    )
                ),
            },
        }

        raw_total_score, breakdown, missing_dims = _weighted_total_and_breakdown(
            dim_scores, dim_extra, eff_weights
        )
        raw_default_score, _, _ = _weighted_total_and_breakdown(
            dim_scores, dim_extra, DEFAULT_WEIGHTS
        )
        low_rating_penalty, low_rating_multiplier = compute_low_rating_penalty(stars, rc)
        price_anom_multiplier = PRICE_ANOMALY_SCORE_MULTIPLIER if p_anom else 1.0
        combined_multiplier = low_rating_multiplier * price_anom_multiplier
        total_score = round(raw_total_score * combined_multiplier, 2)
        default_score = round(raw_default_score * combined_multiplier, 2)
        conf = compute_confidence(item, price_anomaly=p_anom, sales_suspect=s_susp)

        img_url_clean = str(item.get("image_url") or item.get("main_image_url") or "").strip()
        has_explicit_img_field = (
            ("main_image_url" in item)
            or ("image_url" in item)
            or ("photos" in item)
        )
        is_missing_image = (
            ("image_missing" in (item.get("data_quality_issues") or []))
            or (has_explicit_img_field and not img_url_clean)
        )
        missing_dim_count = len(missing_dims)
        is_too_many_missing_dims = missing_dim_count >= 3

        item_record: Dict[str, Any] = {
            "asin": asin,
            "parent_asin": item.get("parent_asin"),
            "title": item.get("title") or "",
            "brand": item.get("brand") or "",
            "image_url": img_url_clean,
            "source_categories": srcs,
            "garment_types": garments,
            "genders": list(item.get("genders") or []),
            "is_cross_category": len(srcs) > 1,
            "cross_category_count": len(srcs),
            "collapsed_count": int(item.get("collapsed_count") or 0),
            "collapsed_sibling_asins": list(item.get("collapsed_sibling_asins") or []),
            "listing_sales_30d": int(round(sales_val)) if sales_val is not None else None,
            "demand_metric_source": demand_source,
            "best_source_category_bsr": dinfo["best_bsr"],
            "price": round(float(raw_price), 2) if (price_valid and raw_price is not None and float(raw_price) > 0) else None,
            "price_valid": bool(price_valid and raw_price is not None and float(raw_price) > 0),
            "price_anomaly": p_anom,
            "sales_suspect": s_susp,
            "sales_suspect_details": s_details,
            "rating_stars": round(float(stars), 2) if stars is not None else None,
            "ratings_count": int(rc) if rc is not None else None,
            "online_date": item.get("online_date"),
            "days_online": days_online,
            "bayesian_rating": round(r_bayes, 3) if r_bayes is not None else None,
            "opportunity_ratio": round(opp_ratio, 3) if opp_ratio is not None else None,
            "automatic_classification": item.get("automatic_classification"),
            "manual_review_status": item.get("manual_review_status"),
            "pre_penalty_score": raw_total_score,
            "low_rating_penalty": low_rating_penalty,
            "low_rating_multiplier": low_rating_multiplier,
            "total_score": total_score,
            "default_score": default_score,
            "missing_dimensions": missing_dims,
            "dimensions": breakdown,
            "confidence_score": conf["score"],
            "confidence_level": conf["level"],
            "confidence_label": conf["label"],
            "confidence_penalties": conf["penalties"],
        }

        if filter_incomplete and (is_missing_image or is_too_many_missing_dims):
            short_parts: List[str] = []
            detail_parts: List[str] = []
            if is_missing_image:
                short_parts.append("缺主图")
                detail_parts.append("缺少商品主图")
            if is_too_many_missing_dims:
                short_parts.append(f"缺 {missing_dim_count} 维")
                dim_names = "、".join(dim_short_by_key.get(k, k) for k in missing_dims)
                detail_parts.append(f"缺失 {missing_dim_count} 维（{dim_names}）")
            unranked_incomplete.append(
                {
                    **item_record,
                    "incomplete_tag": " / ".join(short_parts),
                    "missing_image": is_missing_image,
                    "missing_dimension_count": missing_dim_count,
                    "reason": f"{' / '.join(detail_parts)}，已自动移出主排行榜（防单维极值虚高）",
                }
            )
        else:
            enriched.append(item_record)

    # Default baseline rank (deterministic tie-break: default_score desc, sales desc, best_bsr asc, asin asc)
    default_order = sorted(
        enriched,
        key=lambda x: (
            -x["default_score"],
            -(x["listing_sales_30d"] if x["listing_sales_30d"] is not None else -1),
            (x["best_source_category_bsr"] if x.get("best_source_category_bsr") is not None else 10**9),
            x["asin"],
        ),
    )
    default_rank_map = {item["asin"]: idx + 1 for idx, item in enumerate(default_order)}

    # Active-weight rank
    ranked = sorted(
        enriched,
        key=lambda x: (
            -x["total_score"],
            -(x["listing_sales_30d"] if x["listing_sales_30d"] is not None else -1),
            (x["best_source_category_bsr"] if x.get("best_source_category_bsr") is not None else 10**9),
            x["asin"],
        ),
    )
    prev_map = previous_ranks if isinstance(previous_ranks, dict) else {}
    for idx, item in enumerate(ranked):
        cur_rank = idx + 1
        def_rank = default_rank_map[item["asin"]]
        item["rank"] = cur_rank
        item["default_rank"] = def_rank
        item["rank_delta_vs_default"] = def_rank - cur_rank
        prev_rank = prev_map.get(item["asin"])
        if isinstance(prev_rank, int) and prev_rank > 0:
            item["previous_rank"] = prev_rank
            item["rank_delta_vs_previous"] = prev_rank - cur_rank
        else:
            item["previous_rank"] = None
            item["rank_delta_vs_previous"] = None

    scores_list = [x["total_score"] for x in ranked]
    conf_counts = {"high": 0, "medium": 0, "low": 0}
    for x in ranked:
        conf_counts[x["confidence_level"]] += 1

    weight_sum = sum(eff_weights.values())
    norm_weights = {
        k: round((v / weight_sum * 100.0) if weight_sum > 0 else 0.0, 1)
        for k, v in eff_weights.items()
    }

    return {
        "scoring_version": SCORING_VERSION,
        "observed_date": obs_dt.isoformat(),
        "weights": eff_weights,
        "normalized_weights": norm_weights,
        "price_ranges": eff_prices,
        "default_weights": dict(DEFAULT_WEIGHTS),
        "default_price_ranges": {k: dict(v) for k, v in DEFAULT_PRICE_RANGES.items()},
        "dimensions_meta": DIMENSION_META,
        "rating_median_c": round(rating_c, 3),
        "sales_bsr_audit": bsr_audit["categories"],
        "summary": {
            "scored_count": len(ranked),
            "unranked_no_sales_count": len(unranked_no_sales),
            "unranked_incomplete_count": len(unranked_incomplete),
            "avg_score": round(sum(scores_list) / len(scores_list), 2) if scores_list else None,
            "median_score": round(_median(scores_list), 2) if scores_list else None,
            "confidence_counts": conf_counts,
            "price_anomaly_count": sum(1 for x in ranked if x.get("price_anomaly")),
            "sales_suspect_count": sum(1 for x in ranked if x.get("sales_suspect")),
        },
        "items": ranked,
        "unranked_no_sales": unranked_no_sales,
        "unranked_incomplete": unranked_incomplete,
    }


class ScoringService:
    """Orchestrates Stage 04 candidate scoring over real preprocessed SQLite data + preset storage."""

    def __init__(
        self,
        preprocessing: PreprocessingService,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
    ):
        self.preprocessing = preprocessing
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
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(SCORING_SCHEMA_SQL)
                now = self._now_iso()
                conn.execute(
                    """INSERT OR IGNORE INTO scoring_presets
                       (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                       VALUES ('system_default', '均衡默认', 1, ?, ?, ?, ?)""",
                    (
                        json.dumps(DEFAULT_WEIGHTS, ensure_ascii=False),
                        json.dumps(DEFAULT_PRICE_RANGES, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO scoring_presets
                       (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                       VALUES ('system_evergreen', '常青稳销', 1, ?, ?, ?, ?)""",
                    (
                        json.dumps({"sales": 45.0, "opportunity": 15.0, "rating": 25.0, "freshness": 5.0, "price": 10.0}, ensure_ascii=False),
                        json.dumps(DEFAULT_PRICE_RANGES, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO scoring_presets
                       (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                       VALUES ('system_rising_star', '新品黑马', 1, ?, ?, ?, ?)""",
                    (
                        json.dumps({"sales": 20.0, "opportunity": 35.0, "rating": 10.0, "freshness": 25.0, "price": 10.0}, ensure_ascii=False),
                        json.dumps(DEFAULT_PRICE_RANGES, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO scoring_active_config
                       (id, preset_id, preset_name, weights_json, price_ranges_json, updated_at)
                       VALUES (1, 'system_default', '系统默认方案', ?, ?, ?)""",
                    (
                        json.dumps(DEFAULT_WEIGHTS, ensure_ascii=False),
                        json.dumps(DEFAULT_PRICE_RANGES, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()

    def _serialize_active_config(
        self,
        *,
        preset_id: str,
        preset_name: str,
        weights: Dict[str, float],
        price_ranges: Dict[str, Dict[str, float]],
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        total_w = sum(float(weights.get(k, 0.0)) for k in DIMENSION_KEYS)
        shares = {
            k: round((float(weights.get(k, 0.0)) / total_w) * 100.0, 1) if total_w > 0 else 0.0
            for k in DIMENSION_KEYS
        }
        return {
            "preset_id": preset_id,
            "preset_name": preset_name,
            "is_custom": preset_id == "custom",
            "is_default": preset_id == "system_default",
            "weights": weights,
            "weight_shares": shares,
            "price_ranges": price_ranges,
            "updated_at": updated_at,
        }

    def get_active_config(self) -> Dict[str, Any]:
        """Read persisted workspace active Stage 04 scoring weights & price ranges."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT preset_id, preset_name, weights_json, price_ranges_json, updated_at
                   FROM scoring_active_config WHERE id = 1"""
            ).fetchone()
        if row is None:
            return self._serialize_active_config(
                preset_id="system_default",
                preset_name="系统默认方案",
                weights=dict(DEFAULT_WEIGHTS),
                price_ranges={k: dict(v) for k, v in DEFAULT_PRICE_RANGES.items()},
                updated_at=None,
            )
        weights = validate_weights(json.loads(row["weights_json"]))
        price_ranges = validate_price_ranges(json.loads(row["price_ranges_json"]))
        return self._serialize_active_config(
            preset_id=str(row["preset_id"] or "custom"),
            preset_name=str(row["preset_name"] or "自定义权重"),
            weights=weights,
            price_ranges=price_ranges,
            updated_at=row["updated_at"],
        )

    def set_active_config(
        self,
        *,
        weights: Optional[Dict[str, Any]] = None,
        price_ranges: Optional[Dict[str, Any]] = None,
        preset_id: Optional[str] = None,
        preset_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist workspace active Stage 04 scoring config so 04 -> 05 -> 06 stay in sync."""
        presets = self.list_presets()
        pid_in = str(preset_id or "").strip()
        matched_preset = next((p for p in presets if p["preset_id"] == pid_in), None) if pid_in else None

        if matched_preset is not None and weights is None and price_ranges is None:
            eff_weights = validate_weights(matched_preset["weights"])
            eff_ranges = validate_price_ranges(matched_preset["price_ranges"])
            eff_pid = matched_preset["preset_id"]
            eff_name = matched_preset["name"]
        else:
            cur = self.get_active_config()
            eff_weights = validate_weights(weights if weights is not None else cur["weights"])
            eff_ranges = validate_price_ranges(price_ranges if price_ranges is not None else cur["price_ranges"])
            if matched_preset is not None and (
                eff_weights == validate_weights(matched_preset["weights"])
                and eff_ranges == validate_price_ranges(matched_preset["price_ranges"])
            ):
                eff_pid = matched_preset["preset_id"]
                eff_name = matched_preset["name"]
            elif (
                eff_weights == validate_weights(DEFAULT_WEIGHTS)
                and eff_ranges == validate_price_ranges(DEFAULT_PRICE_RANGES)
                and pid_in in ("", "system_default")
            ):
                eff_pid = "system_default"
                eff_name = "系统默认方案"
            else:
                eff_pid = "custom"
                eff_name = str(preset_name or "自定义权重").strip()[:48] or "自定义权重"

        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
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
                    (
                        eff_pid,
                        eff_name,
                        json.dumps(eff_weights, ensure_ascii=False),
                        json.dumps(eff_ranges, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()
        return self._serialize_active_config(
            preset_id=eff_pid,
            preset_name=eff_name,
            weights=eff_weights,
            price_ranges=eff_ranges,
            updated_at=now,
        )

    def list_presets(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at
                   FROM scoring_presets
                   ORDER BY is_default DESC, updated_at DESC"""
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "preset_id": r["preset_id"],
                    "name": r["name"],
                    "is_default": bool(r["is_default"]),
                    "weights": json.loads(r["weights_json"]),
                    "price_ranges": json.loads(r["price_ranges_json"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
        return out

    def save_preset(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ScoringValidationError("请求体必须为 JSON 对象。")
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 48:
            raise ScoringValidationError("方案名称不能为空且不超过 48 个字符。")
        preset_id = str(payload.get("preset_id") or "").strip()
        if preset_id == "system_default":
            raise ScoringValidationError("系统默认方案不可覆盖，请另存为新方案。")
        if not preset_id:
            preset_id = f"preset_{secrets.token_hex(6)}"

        weights = validate_weights(payload.get("weights"))
        price_ranges = validate_price_ranges(payload.get("price_ranges"))
        now = self._now_iso()

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO scoring_presets
                       (preset_id, name, is_default, weights_json, price_ranges_json, created_at, updated_at)
                       VALUES (?, ?, 0, ?, ?, ?, ?)
                       ON CONFLICT(preset_id) DO UPDATE SET
                         name = excluded.name,
                         weights_json = excluded.weights_json,
                         price_ranges_json = excluded.price_ranges_json,
                         updated_at = excluded.updated_at""",
                    (
                        preset_id,
                        name,
                        json.dumps(weights, ensure_ascii=False),
                        json.dumps(price_ranges, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
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
                    (
                        preset_id,
                        name,
                        json.dumps(weights, ensure_ascii=False),
                        json.dumps(price_ranges, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()
        return {
            "preset": {
                "preset_id": preset_id,
                "name": name,
                "is_default": False,
                "weights": weights,
                "price_ranges": price_ranges,
                "updated_at": now,
            },
            "active_config": self.get_active_config(),
            "presets": self.list_presets(),
        }

    def delete_preset(self, preset_id: str) -> Dict[str, Any]:
        pid = (preset_id or "").strip()
        if not pid:
            raise ScoringValidationError("缺少 preset_id。")
        if pid == "system_default":
            raise ScoringValidationError("系统默认方案不可删除。")
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM scoring_presets WHERE preset_id = ? AND is_default = 0", (pid,))
                if cur.rowcount == 0:
                    raise ScoringNotFoundError(f"未找到评分方案：{pid}。")
                conn.execute(
                    """UPDATE scoring_active_config
                       SET preset_id = 'custom', preset_name = '自定义权重', updated_at = ?
                       WHERE id = 1 AND preset_id = ?""",
                    (self._now_iso(), pid),
                )
                conn.commit()
        return {"deleted": pid, "active_config": self.get_active_config(), "presets": self.list_presets()}

    def evaluate(
        self,
        *,
        observed_date: Optional[str] = None,
        category: str = "all",
        confidence: str = "all",
        q: str = "",
        weights: Optional[Dict[str, Any]] = None,
        price_ranges: Optional[Dict[str, Any]] = None,
        previous_ranks: Optional[Dict[str, int]] = None,
        persist_active: bool = False,
        active_preset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Evaluate Stage 04 dynamic candidate scores over real preprocessed `ready` products."""
        if persist_active:
            active_cfg = self.set_active_config(
                weights=weights,
                price_ranges=price_ranges,
                preset_id=active_preset_id,
            )
            eff_weights = active_cfg["weights"]
            eff_price_ranges = active_cfg["price_ranges"]
        elif weights is None and price_ranges is None:
            active_cfg = self.get_active_config()
            eff_weights = active_cfg["weights"]
            eff_price_ranges = active_cfg["price_ranges"]
        else:
            active_cfg = self.get_active_config()
            eff_weights = validate_weights(weights)
            eff_price_ranges = validate_price_ranges(price_ranges)

        db_status = self.preprocessing.repo.status()
        available_dates = self.preprocessing.repo.get_available_dates()
        presets = self.list_presets()
        if db_status.state != "ready":
            return {
                "ready": False,
                "db_status": db_status.to_dict(),
                "selected_date": observed_date,
                "available_dates": available_dates,
                "categories": serialize_categories(),
                "presets": presets,
                "active_config": active_cfg,
                "weights": eff_weights,
                "price_ranges": eff_price_ranges,
                "default_weights": dict(DEFAULT_WEIGHTS),
                "default_price_ranges": {k: dict(v) for k, v in DEFAULT_PRICE_RANGES.items()},
                "dimensions_meta": DIMENSION_META,
                "summary": {
                    "scored_count": 0,
                    "filtered_count": 0,
                    "unranked_no_sales_count": 0,
                    "unranked_incomplete_count": 0,
                    "avg_score": None,
                    "median_score": None,
                    "confidence_counts": {"high": 0, "medium": 0, "low": 0},
                },
                "items": [],
                "unranked_no_sales": [],
                "unranked_incomplete": [],
            }

        target_date = self.preprocessing._ensure_preprocessed(observed_date)
        all_hydrated = self.preprocessing._hydrate_rows(target_date)
        # Strictly only `ready` (and not excluded) enter Stage 04 unified 6-category competition
        ready_items = [
            i for i in all_hydrated
            if i["eligibility"] == "ready" and i["manual_review_status"] != "confirmed_exclude"
        ]

        # Score over the full unified 6-category ready pool FIRST so ranks & normalization
        # reflect true cross-category competition; then apply view filters (category/confidence/search).
        scored = compute_candidate_scores(
            ready_items,
            observed_date=target_date,
            weights=eff_weights,
            price_ranges=eff_price_ranges,
            previous_ranks=previous_ranks,
            bsr_reference_pool=all_hydrated,
        )

        filtered_items = scored["items"]
        filtered_unranked = scored["unranked_no_sales"]
        filtered_incomplete = scored["unranked_incomplete"]

        if category and category != "all":
            spec = resolve_category(category)
            target_node = spec.node_id if (spec and spec.is_configured and spec.node_id) else category
            filtered_items = [
                i for i in filtered_items
                if any(
                    s.get("node_id") == target_node or s.get("category_key") == category
                    for s in i["source_categories"]
                )
            ]
            filtered_unranked = [
                i for i in filtered_unranked
                if any(
                    s.get("node_id") == target_node or s.get("category_key") == category
                    for s in i["source_categories"]
                )
            ]
            filtered_incomplete = [
                i for i in filtered_incomplete
                if any(
                    s.get("node_id") == target_node or s.get("category_key") == category
                    for s in i["source_categories"]
                )
            ]

        conf_filter = (confidence or "all").strip().lower()
        if conf_filter in ("high", "medium", "low"):
            filtered_items = [i for i in filtered_items if i["confidence_level"] == conf_filter]

        query = (q or "").strip().lower()
        if query:
            filtered_items = [
                i for i in filtered_items
                if query in i["asin"].lower()
                or query in (i["title"] or "").lower()
                or query in (i["brand"] or "").lower()
            ]

        scored["summary"]["filtered_count"] = len(filtered_items)
        return {
            "ready": True,
            "db_status": db_status.to_dict(),
            "selected_date": target_date,
            "available_dates": available_dates,
            "categories": serialize_categories(),
            "presets": presets,
            "active_config": active_cfg,
            "category_filter": category,
            "confidence_filter": conf_filter,
            "q": q,
            **scored,
            "items": filtered_items,
            "unranked_no_sales": filtered_unranked,
            "unranked_incomplete": filtered_incomplete,
        }
