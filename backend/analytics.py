"""Rigorous market analytics, distributions, data quality metrics, and server-side filtering."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    CATEGORIES,
    CATEGORY_BY_NODE,
    CONFIGURED_CATEGORIES,
    METHODOLOGY_NOTES,
    PENDING_CATEGORIES,
    resolve_category,
    serialize_categories,
)
from .keywords import CANONICAL_MAP, extract_keyword_cloud

# Known public apparel / pop-culture brand official domains for optional favicon badge display.
# Note: Sorftime API does NOT return brand logo URLs; Amazon private-label sellers without
# public websites fall back cleanly to deterministic colored monogram badges.
KNOWN_BRAND_DOMAINS: Dict[str, str] = {
    "gildan": "gildan.com",
    "hanes": "hanes.com",
    "carhartt": "carhartt.com",
    "fruit of the loom": "fruit.com",
    "champion": "champion.com",
    "under armour": "underarmour.com",
    "nike": "nike.com",
    "adidas": "adidas.com",
    "amazon essentials": "amazon.com",
    "columbia": "columbia.com",
    "levi's": "levi.com",
    "dickies": "dickies.com",
    "wrangler": "wrangler.com",
    "comfort colors": "comfortcolors.com",
    "russell athletic": "russellathletic.com",
    "jerzees": "jerzees.com",
    "popfunk": "popfunk.com",
    "disney": "disney.com",
    "marvel": "marvel.com",
    "star wars": "starwars.com",
    "nintendo": "nintendo.com",
    "peanuts": "peanuts.com",
    "care bears": "carebears.com",
    "gap": "gap.com",
    "old navy": "oldnavy.com",
    "eddie bauer": "eddiebauer.com",
    "nautica": "nautica.com",
    "calvin klein": "calvinklein.us",
    "tommy hilfiger": "tommy.com",
    "ann arbor t-shirt co.": "annarbortees.com",
    "feelin good tees": "feelingoodtees.com",
    "crazy dog t-shirts": "crazydogtshirts.com",
    "ripple junction": "ripplejunction.com",
    "fifth sun": "fifthsun.com",
    "bioworld": "bioworldmerch.com",
    "hybrid apparel": "hybridapparel.com",
    "bella + canvas": "bellacanvas.com",
    "next level apparel": "nextlevelapparel.com",
}

BRAND_AVATAR_PALETTE: Tuple[Tuple[str, str], ...] = (
    ("#3B6BF6", "#EFF4FF"),
    ("#E14D7A", "#FDF2F6"),
    ("#0D9488", "#F0FDFA"),
    ("#7C3AED", "#F5F3FF"),
    ("#EA580C", "#FFF7ED"),
    ("#0284C7", "#F0F9FF"),
    ("#059669", "#ECFDF5"),
    ("#D97706", "#FFFBEB"),
)


def _brand_visual_meta(brand_name: str) -> Dict[str, Any]:
    clean = (brand_name or "").strip()
    lower = clean.lower()
    domain = KNOWN_BRAND_DOMAINS.get(lower)
    if not domain:
        for key, dom in KNOWN_BRAND_DOMAINS.items():
            if lower.startswith(key + " ") or key == lower.rstrip("."):
                domain = dom
                break
    words = [w for w in clean.replace("-", " ").replace("_", " ").split() if w]
    if not words or clean == "未知品牌":
        initials = "?"
    elif len(words) == 1:
        initials = words[0][:2].upper()
    else:
        initials = (words[0][0] + words[1][0]).upper()
    digest = int(hashlib.md5(lower.encode("utf-8")).hexdigest()[:6], 16)
    fg, bg = BRAND_AVATAR_PALETTE[digest % len(BRAND_AVATAR_PALETTE)]
    return {
        "logo_domain": domain,
        "logo_url": f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else None,
        "brand_initials": initials,
        "brand_color": fg,
        "brand_bg": bg,
    }


def percentile(sorted_values: List[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * p
    low = math.floor(k)
    high = min(low + 1, len(sorted_values) - 1)
    weight = k - low
    return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * weight)


def summarize_distribution(values: List[float], decimals: int = 1) -> Dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
            "mean": None,
        }
    s = sorted(values)
    return {
        "count": len(s),
        "min": round(s[0], decimals),
        "p10": round(percentile(s, 0.10), decimals),
        "p25": round(percentile(s, 0.25), decimals),
        "median": round(percentile(s, 0.50), decimals),
        "p75": round(percentile(s, 0.75), decimals),
        "p90": round(percentile(s, 0.90), decimals),
        "max": round(s[-1], decimals),
        "mean": round(sum(s) / len(s), decimals),
    }


SALES_BUCKETS: Tuple[Tuple[str, int, Optional[int]], ...] = (
    ("< 500", 0, 500),
    ("500–999", 500, 1000),
    ("1k–2k", 1000, 2000),
    ("2k–5k", 2000, 5000),
    ("5k+", 5000, None),
)

SALES_BUCKETS_WIDE: Tuple[Tuple[str, int, Optional[int]], ...] = (
    ("< 1k", 0, 1000),
    ("1k–2k", 1000, 2000),
    ("2k–5k", 2000, 5000),
    ("5k–10k", 5000, 10000),
    ("10k–25k", 10000, 25000),
    ("25k–50k", 25000, 50000),
    ("50k+", 50000, None),
)

PRICE_BANDS: Tuple[Tuple[str, str, float, Optional[float]], ...] = (
    ("under_15", "< $15", 0.0, 15.0),
    ("15_20", "$15 - $19.99", 15.0, 20.0),
    ("20_25", "$20 - $24.99", 20.0, 25.0),
    ("25_35", "$25 - $34.99", 25.0, 35.0),
    ("35_plus", "$35+", 35.0, None),
)


def _match_price_band(price: Optional[float], price_valid: bool, band_key: str) -> bool:
    if band_key == "invalid":
        return not price_valid
    if not price_valid or price is None:
        return False
    for key, _, low, high in PRICE_BANDS:
        if key == band_key:
            return price >= low and (high is None or price < high)
    return True


def build_market_overview(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Compute complete market dashboard metrics strictly respecting business rules."""
    status = scope["status"]
    if status.state != "ready":
        return {
            "db_status": status.to_dict(),
            "ready": False,
            "selected_date": scope.get("selected_date"),
            "available_dates": scope.get("available_dates", []),
            "category_filter": scope.get("category_filter", "all"),
            "methodology": METHODOLOGY_NOTES,
        }

    products: List[Dict[str, Any]] = scope["products"]
    asin_products: List[Dict[str, Any]] = scope.get("asin_products", products)
    dup_meta: Dict[str, Any] = scope.get("duplicate_meta", {})
    dedup_mode: str = scope.get("dedup_mode", "listing")
    observations: List[Dict[str, Any]] = scope["observations"]
    all_date_obs: List[Dict[str, Any]] = scope["all_date_observations"]
    batches: List[Dict[str, Any]] = scope["batches"]
    all_date_batches: List[Dict[str, Any]] = scope.get("all_date_batches", batches)
    norm_cat: str = scope["category_filter"]

    unique_count = len(products)
    raw_asin_count = len(asin_products)
    obs_count = len(observations)
    dup_cluster_count = int(dup_meta.get("cluster_count", 0))
    collapsed_dup_count = int(dup_meta.get("collapsed_asin_count", 0))
    latest_fetched_at = max((b["fetched_at"] for b in batches), default=None)

    # Sales distribution (Listing 30d estimated sales; strictly deduped by ASIN)
    sales_values = [
        float(p["listing_sales_30d"])
        for p in products
        if p["listing_sales_30d"] is not None
    ]
    sales_stats = summarize_distribution(sales_values, decimals=1)
    # Top-10 concentration share (carefully scoped as share of deduped sample estimates, NOT total market)
    sorted_sales_desc = sorted(sales_values, reverse=True)
    sum_dedup_sales = sum(sorted_sales_desc)
    top10_share = (
        round(sum(sorted_sales_desc[:10]) / sum_dedup_sales * 100.0, 1)
        if sum_dedup_sales > 0
        else None
    )

    use_wide_sales_buckets = bool(
        sales_stats["p90"]
        and sales_stats["p90"] > 4500
        and sales_stats["max"]
        and sales_stats["max"] >= 25000
    )
    active_sales_buckets = SALES_BUCKETS_WIDE if use_wide_sales_buckets else SALES_BUCKETS
    sales_histogram: List[Dict[str, Any]] = []
    for label, low, high in active_sales_buckets:
        matched = [
            p
            for p in products
            if p["listing_sales_30d"] is not None
            and p["listing_sales_30d"] >= low
            and (high is None or p["listing_sales_30d"] < high)
        ]
        sales_histogram.append(
            {
                "label": label,
                "min": low,
                "max": high,
                "count": len(matched),
                "share_pct": round(len(matched) / len(sales_values) * 100.0, 1) if sales_values else 0.0,
            }
        )

    # Price distribution (strictly excluding $0 and missing)
    valid_price_products = [p for p in products if p["price_valid"] and p["sales_price_usd"] is not None]
    valid_prices = [float(p["sales_price_usd"]) for p in valid_price_products]
    price_stats = summarize_distribution(valid_prices, decimals=2)
    invalid_price_count = unique_count - len(valid_price_products)

    price_distribution: List[Dict[str, Any]] = []
    for key, label, low, high in PRICE_BANDS:
        band_items = [
            p
            for p in valid_price_products
            if p["sales_price_usd"] >= low and (high is None or p["sales_price_usd"] < high)
        ]
        band_sales = [
            float(p["listing_sales_30d"])
            for p in band_items
            if p["listing_sales_30d"] is not None
        ]
        price_distribution.append(
            {
                "key": key,
                "label": label,
                "min": low,
                "max": high,
                "count": len(band_items),
                "share_pct": (
                    round(len(band_items) / len(valid_price_products) * 100.0, 1)
                    if valid_price_products
                    else 0.0
                ),
                "median_sales_30d": round(percentile(sorted(band_sales), 0.5), 1) if band_sales else None,
            }
        )

    # Price vs 30d Listing Sales scatter points (only valid price & valid sales)
    scatter_points: List[Dict[str, Any]] = []
    for p in valid_price_products:
        if p["listing_sales_30d"] is None:
            continue
        if p["is_cross_category"]:
            cat_group = "both"
            cat_label = f"跨 {p.get('cross_category_count', 2)} 类目重合"
            cat_color = "#7C3AED"
        else:
            first_src = p["source_categories"][0] if p["source_categories"] else None
            cat_group = first_src["category_key"] if first_src else "unknown"
            cat_label = first_src["source_category"] if first_src else "未知"
            cat_color = first_src.get("color", "#3B6BF6") if first_src else "#3B6BF6"
        scatter_points.append(
            {
                "asin": p["asin"],
                "title": p["title"],
                "brand": p["brand"],
                "price_usd": p["sales_price_usd"],
                "sales_30d": p["listing_sales_30d"],
                "rating_stars": p["rating_stars"],
                "ratings_count": p["ratings_count"],
                "category_group": cat_group,
                "category_label": cat_label,
                "category_color": cat_color,
                "needs_parent_verification": p["needs_parent_verification"],
            }
        )

    # Ratings coverage
    valid_ratings = [float(p["rating_stars"]) for p in products if p["rating_status"] == "valid"]
    rating_stats = summarize_distribution(valid_ratings, decimals=2)

    # Parent ASIN integrity check
    parent_groups: Dict[str, List[str]] = {}
    for p in products:
        if p["parent_asin"]:
            parent_groups.setdefault(p["parent_asin"], []).append(p["asin"])
    multi_child_parents = sum(1 for asins in parent_groups.values() if len(asins) > 1)

    # Multi-category comparison (dynamically computed across all 6 configured/pending categories + any DB nodes)
    cat_asins: Dict[str, set] = {}
    cat_obs_map: Dict[str, List[Dict[str, Any]]] = {}
    asin_nodes_map: Dict[str, set] = {}
    for obs in all_date_obs:
        nid = obs["node_id"]
        cat_asins.setdefault(nid, set()).add(obs["asin"])
        cat_obs_map.setdefault(nid, []).append(obs)
        asin_nodes_map.setdefault(obs["asin"], set()).add(nid)

    cross_cat_asins = {a for a, nodes in asin_nodes_map.items() if len(nodes) > 1}
    single_cat_asins = {a for a, nodes in asin_nodes_map.items() if len(nodes) == 1}

    def _build_cat_card(
        *,
        key: str,
        node_id: Optional[str],
        name: str,
        short_name: str,
        en_name: str,
        garment_type: str,
        garment_label: str,
        gender: str,
        gender_label: str,
        segment: str,
        is_configured: bool,
        node_status: str,
        status_label: str,
        color: str,
        soft_color: str,
    ) -> Dict[str, Any]:
        c_obs = cat_obs_map.get(node_id, []) if node_id else []
        c_asin_set = cat_asins.get(node_id, set()) if node_id else set()
        exclusive_count = sum(1 for a in c_asin_set if a in single_cat_asins)
        overlap_count = sum(1 for a in c_asin_set if a in cross_cat_asins)
        c_sales = [float(o["listing_sales_30d"]) for o in c_obs if o["listing_sales_30d"] is not None]
        c_prices = [float(o["sales_price_usd"]) for o in c_obs if o["price_valid"] and o["sales_price_usd"] is not None]
        c_ratings = [float(o["rating_stars"]) for o in c_obs if o["rating_status"] == "valid"]
        c_bsr_valid = sum(1 for o in c_obs if o["source_category_bsr"] is not None)
        c_batch = next((b for b in all_date_batches if node_id and b["node_id"] == node_id), None)
        return {
            "key": key,
            "node_id": node_id,
            "name": name,
            "short_name": short_name,
            "en_name": en_name,
            "garment_type": garment_type,
            "garment_label": garment_label,
            "gender": gender,
            "gender_label": gender_label,
            "segment": segment,
            "is_configured": is_configured,
            "node_status": node_status,
            "status_label": status_label,
            "has_data": len(c_obs) > 0,
            "color": color,
            "soft_color": soft_color,
            "observation_count": len(c_obs),
            "unique_asin_count": len(c_asin_set),
            "exclusive_asin_count": exclusive_count,
            "overlap_asin_count": overlap_count,
            "sales_median": round(percentile(sorted(c_sales), 0.5), 1) if c_sales else None,
            "sales_p75": round(percentile(sorted(c_sales), 0.75), 1) if c_sales else None,
            "sales_max": int(max(c_sales)) if c_sales else None,
            "price_valid_count": len(c_prices),
            "price_invalid_count": len(c_obs) - len(c_prices),
            "price_median": round(percentile(sorted(c_prices), 0.5), 2) if c_prices else None,
            "rating_valid_count": len(c_ratings),
            "rating_median": round(percentile(sorted(c_ratings), 0.5), 2) if c_ratings else None,
            "source_bsr_valid_count": c_bsr_valid,
            "source_bsr_missing_count": len(c_obs) - c_bsr_valid,
            "page_collected": c_batch["page"] if c_batch else None,
            "total_pages_reported": c_batch["page_count"] if c_batch else None,
        }

    category_cards: List[Dict[str, Any]] = [
        _build_cat_card(
            key=spec.key,
            node_id=spec.node_id,
            name=spec.name,
            short_name=spec.short_name,
            en_name=spec.en_name,
            garment_type=spec.garment_type,
            garment_label=spec.garment_label,
            gender=spec.gender,
            gender_label=spec.gender_label,
            segment=spec.segment,
            is_configured=spec.is_configured,
            node_status=spec.node_status,
            status_label=spec.status_label,
            color=spec.color,
            soft_color=spec.soft_color,
        )
        for spec in CATEGORIES
    ]

    # Include any extra DB node_ids not yet in CATEGORIES (e.g., synthetic multi-category test fixtures)
    known_nodes = {c.node_id for c in CATEGORIES if c.node_id}
    extra_palette = ("#F59E0B", "#10B981", "#6366F1", "#EC4899")
    for idx, extra_nid in enumerate(sorted(n for n in cat_asins if n not in known_nodes)):
        sample_obs = cat_obs_map[extra_nid][0]
        color = extra_palette[idx % len(extra_palette)]
        category_cards.append(
            _build_cat_card(
                key=f"node_{extra_nid}",
                node_id=extra_nid,
                name=sample_obs["source_category"],
                short_name=sample_obs["source_category"][:6],
                en_name=sample_obs["source_category"],
                garment_type=sample_obs.get("garment_type", "unknown"),
                garment_label=sample_obs.get("garment_label", "未分类"),
                gender=sample_obs.get("gender", "unknown"),
                gender_label=sample_obs.get("gender_label", "未知"),
                segment="other",
                is_configured=True,
                node_status="configured",
                status_label="已入库节点",
                color=color,
                soft_color="#F8FAFC",
            )
        )

    active_category_cards = [c for c in category_cards if c["has_data"]]
    overlap_segments: List[Dict[str, Any]] = [
        {
            "key": c["key"],
            "label": f"{c['short_name']}独有",
            "full_label": f"仅 {c['name']}",
            "count": c["exclusive_asin_count"],
            "color": c["color"],
        }
        for c in active_category_cards
        if c["exclusive_asin_count"] > 0
    ]
    if cross_cat_asins:
        overlap_segments.append(
            {
                "key": "cross_category",
                "label": "跨类目重合",
                "full_label": "跨类目重合 ASIN（已去重）",
                "count": len(cross_cat_asins),
                "color": "#7C3AED",
            }
        )

    women_sweat_card = next((c for c in category_cards if c["key"] == "womens_novelty_sweatshirts"), None)
    men_sweat_card = next((c for c in category_cards if c["key"] == "mens_novelty_sweatshirts"), None)

    # Top-selling sample showcase (8 items in current scope, sorted by listing_sales_30d desc)
    hot_samples = sorted(
        products,
        key=lambda p: (
            -(p["listing_sales_30d"] if p["listing_sales_30d"] is not None else -1),
            p["asin"],
        ),
    )[:8]

    # Top brands in current deduped scope (enriched with brand emblem/logo metadata)
    brand_map: Dict[str, Dict[str, Any]] = {}
    for p in products:
        brand_name = p["brand"] or "未知品牌"
        entry = brand_map.setdefault(
            brand_name,
            {
                "brand": brand_name,
                "asin_count": 0,
                "sales_list": [],
                "cross_cat_count": 0,
                "has_brand_store": False,
                "sample_image_url": None,
                "best_sales": -1,
            },
        )
        entry["asin_count"] += 1
        if p["listing_sales_30d"] is not None:
            entry["sales_list"].append(float(p["listing_sales_30d"]))
            if p["listing_sales_30d"] > entry["best_sales"] and p.get("main_image_url"):
                entry["best_sales"] = p["listing_sales_30d"]
                entry["sample_image_url"] = p["main_image_url"]
        elif not entry["sample_image_url"] and p.get("main_image_url"):
            entry["sample_image_url"] = p["main_image_url"]
        if p.get("has_brand_store"):
            entry["has_brand_store"] = True
        if p["is_cross_category"]:
            entry["cross_cat_count"] += 1

    top_brands = sorted(
        [
            {
                "brand": b["brand"],
                "asin_count": b["asin_count"],
                "share_pct": round(b["asin_count"] / unique_count * 100.0, 1) if unique_count else 0.0,
                "median_sales_30d": round(percentile(sorted(b["sales_list"]), 0.5), 1)
                if b["sales_list"]
                else None,
                "max_sales_30d": int(max(b["sales_list"])) if b["sales_list"] else None,
                "cross_cat_count": b["cross_cat_count"],
                "has_brand_store": b["has_brand_store"],
                "sample_image_url": b["sample_image_url"],
                **_brand_visual_meta(b["brand"]),
            }
            for b in brand_map.values()
        ],
        key=lambda x: (-x["asin_count"], -(x["median_sales_30d"] or 0)),
    )[:8]

    # Data quality breakdown (deduped ASIN basis + observation-level BSR transparency)
    obs_missing_src_bsr = sum(1 for o in observations if o["source_category_bsr"] is None)
    obs_valid_src_bsr = obs_count - obs_missing_src_bsr
    asin_missing_src_bsr = sum(1 for p in products if "missing_source_bsr" in p["anomalies"])
    asin_same_parent = sum(1 for p in products if p["needs_parent_verification"])
    asin_missing_rating = sum(1 for p in products if p["rating_status"] != "valid")
    asin_missing_image = sum(1 for p in products if not p["main_image_url"])
    asin_missing_link = sum(1 for p in products if not p["amazon_url"])
    asin_cross_cat = sum(1 for p in products if p["is_cross_category"])

    quality_checks = [
        {
            "key": "duplicate_listing_cluster",
            "label": "同款/同介绍重复簇",
            "count": dup_cluster_count,
            "denominator": unique_count,
            "collapsed_asin_count": collapsed_dup_count,
            "severity": "info",
            "filter_value": "duplicate_listing_cluster",
            "note": (
                f"检出 {dup_cluster_count} 组同品牌商品介绍/标题完全一致或同图孤立变体重复（折叠 {collapsed_dup_count} 个重复 ASIN）；"
                + ("当前已归并至主代表款，绝不累加销量。" if dedup_mode == "listing" else "当前处于仅 ASIN 模式，已标出供核对。")
            ),
        },
        {
            "key": "cross_category",
            "label": "跨类目重合 ASIN",
            "count": asin_cross_cat,
            "denominator": unique_count,
            "severity": "info",
            "filter_value": "cross_category",
            "note": "同时出现在多个目标类目；看板已按 ASIN 去重，未重复累加销量。",
        },
        {
            "key": "invalid_price",
            "label": "无效 / $0 售价",
            "count": invalid_price_count,
            "denominator": unique_count,
            "severity": "warning",
            "filter_value": "invalid_price",
            "note": "源数据返回 0 或缺失；已从价格统计和散点图中隔离，未补值。",
        },
        {
            "key": "missing_rating",
            "label": "缺失星级评分",
            "count": asin_missing_rating,
            "denominator": unique_count,
            "severity": "warning",
            "filter_value": "missing_rating",
            "note": "接口返回 0 或空值（多见于刚上架新品）；保留为空，不按 0 星计入均分。",
        },
        {
            "key": "same_parent_asin",
            "label": "父子编号相同待核验",
            "count": asin_same_parent,
            "denominator": unique_count,
            "severity": "warning",
            "filter_value": "same_parent_asin",
            "note": "ParentAsin = Asin，已保留原记录并隐藏直达链接，需后续人工核验。",
        },
        {
            "key": "missing_source_bsr",
            "label": "缺失源类目 BSR",
            "count": asin_missing_src_bsr,
            "denominator": unique_count,
            "obs_count": obs_missing_src_bsr,
            "obs_denominator": obs_count,
            "severity": "neutral",
            "filter_value": "missing_source_bsr",
            "note": f"当前范围 {obs_missing_src_bsr}/{obs_count} 条类目观察未在 BsrCategory 命中对应源类目排名。",
        },
        {
            "key": "missing_image_or_link",
            "label": "缺主图 / 链接不可用",
            "count": sum(1 for p in products if not p["main_image_url"] or not p["amazon_url"]),
            "denominator": unique_count,
            "missing_image_count": asin_missing_image,
            "missing_link_count": asin_missing_link,
            "severity": "neutral",
            "filter_value": "missing_link",
            "note": f"无主图 {asin_missing_image} 件；因父子同号或规则置空链接 {asin_missing_link} 件。",
        },
    ]

    # Single-day snapshot warning
    distinct_dates_count = len(scope["available_dates"])
    cat_pending = bool(scope.get("category_pending_config", False))
    resolved_spec = resolve_category(norm_cat) if norm_cat != "all" else None
    if norm_cat == "all":
        scope_name = "全部已采类目（去重）"
    elif resolved_spec is not None:
        scope_name = f"{resolved_spec.name}（待配置 Node ID）" if cat_pending else resolved_spec.name
    else:
        scope_name = next((c["name"] for c in category_cards if c["node_id"] == norm_cat), f"类目 {norm_cat}")

    active_names = "、".join(
        f"{c['name']}（{c['node_id']}）" for c in active_category_cards if c.get("node_id")
    )
    active_cat_count = len(active_category_cards)

    return {
        "db_status": status.to_dict(),
        "ready": True,
        "selected_date": scope["selected_date"],
        "available_dates": scope["available_dates"],
        "distinct_dates_count": distinct_dates_count,
        "category_filter": norm_cat,
        "category_pending_config": cat_pending,
        "prep_scope": scope.get("prep_scope", "all"),
        "prep_filter_summary": scope.get("prep_filter_summary"),
        "categories": serialize_categories(),
        "dedup_mode": dedup_mode,
        "scope_name": scope_name,
        "deduplication": {
            "mode": dedup_mode,
            "observation_count": obs_count,
            "raw_asin_count": raw_asin_count,
            "unique_listing_count": unique_count if dedup_mode == "listing" else raw_asin_count - collapsed_dup_count,
            "duplicate_cluster_count": dup_cluster_count,
            "collapsed_duplicate_asin_count": collapsed_dup_count if dedup_mode == "listing" else 0,
            "detected_duplicate_asin_count": collapsed_dup_count,
            "clusters": dup_meta.get("clusters", []),
        },
        "can_sort_source_bsr": norm_cat != "all" and not cat_pending,
        "latest_fetched_at": latest_fetched_at,
        "sample_context": {
            "domain": 1,
            "domain_label": "Amazon US (美国站)",
            "is_single_day_snapshot": distinct_dates_count <= 1,
            "is_first_page_only": all((b.get("page") == 1) for b in all_date_batches),
            "category_count": len({b["node_id"] for b in all_date_batches}),
            "configured_category_count": len(CONFIGURED_CATEGORIES),
            "total_target_categories": len(CATEGORIES),
            "pending_category_count": len(PENDING_CATEGORIES),
            "disclosure": (
                f"当前数据覆盖 {active_cat_count} / {len(CATEGORIES)} 个目标类目的首页快照（每类最多 100 条），"
                "Listing 近 30 天销量为第三方估算值，不代表全美市场总规模、真实订单量或销量增长趋势。"
            ),
        },
        "kpis": {
            "observation_count": obs_count,
            "raw_asin_count": raw_asin_count,
            "unique_asin_count": unique_count,
            "duplicate_cluster_count": dup_cluster_count,
            "collapsed_duplicate_asin_count": collapsed_dup_count if dedup_mode == "listing" else 0,
            "detected_duplicate_asin_count": collapsed_dup_count,
            "distinct_parent_count": len(parent_groups),
            "multi_child_parent_groups": multi_child_parents,
            "cross_category_overlap_count": len(cross_cat_asins),
            "category_count": len({b["node_id"] for b in batches}),
            "configured_category_count": len(CONFIGURED_CATEGORIES),
            "total_target_categories": len(CATEGORIES),
            "sales_coverage_count": len(sales_values),
            "sales_coverage_pct": round(len(sales_values) / unique_count * 100.0, 1) if unique_count else 0.0,
            "price_valid_count": len(valid_price_products),
            "price_valid_pct": round(len(valid_price_products) / unique_count * 100.0, 1) if unique_count else 0.0,
            "price_invalid_count": invalid_price_count,
            "rating_valid_count": len(valid_ratings),
            "rating_valid_pct": round(len(valid_ratings) / unique_count * 100.0, 1) if unique_count else 0.0,
            "source_bsr_valid_obs": obs_valid_src_bsr,
            "source_bsr_total_obs": obs_count,
            "source_bsr_coverage_pct": round(obs_valid_src_bsr / obs_count * 100.0, 1) if obs_count else 0.0,
            "sales_median": sales_stats["median"],
            "sales_p25": sales_stats["p25"],
            "sales_p75": sales_stats["p75"],
            "sales_p90": sales_stats["p90"],
            "sales_max": sales_stats["max"],
            "top10_sample_share_pct": top10_share,
            "price_median": price_stats["median"],
            "price_p25": price_stats["p25"],
            "price_p75": price_stats["p75"],
            "rating_median": rating_stats["median"],
            "distinct_brand_count": len(brand_map),
            "latest_observed_date": scope["selected_date"],
        },
        "sales_analysis": {
            "stats": sales_stats,
            "histogram": sales_histogram,
            "bucket_scale": "wide_7tier" if use_wide_sales_buckets else "compact_5tier",
            "top10_sample_share_pct": top10_share,
            "note": "按去重 ASIN 统计第三方 Listing 近 30 天估算销量分布；未将跨类目重复样本或父子体销量叠加。",
        },
        "price_analysis": {
            "stats": price_stats,
            "valid_count": len(valid_price_products),
            "excluded_invalid_count": invalid_price_count,
            "bands": price_distribution,
            "scatter": scatter_points,
            "note": f"已剔除 {invalid_price_count} 件 $0 或缺失价格样本，以下分布基于有效售价样本 n={len(valid_price_products)}。",
        },
        "category_comparison": {
            "categories": category_cards,
            "active_categories": active_category_cards,
            "pending_categories": [c for c in category_cards if not c["is_configured"]],
            "overlap": {
                "women_only": women_sweat_card["exclusive_asin_count"] if women_sweat_card else 0,
                "both": len(cross_cat_asins),
                "men_only": men_sweat_card["exclusive_asin_count"] if men_sweat_card else 0,
                "cross_category_asin_count": len(cross_cat_asins),
                "single_category_asin_count": len(single_cat_asins),
                "active_category_count": active_cat_count,
                "configured_category_count": len(CONFIGURED_CATEGORIES),
                "pending_category_count": len(PENDING_CATEGORIES),
                "total_target_categories": len(CATEGORIES),
                "total_unique": len(asin_nodes_map),
                "total_observations": len(all_date_obs),
                "segments": overlap_segments,
            },
            "bsr_warning": (
                f"{active_names or '不同类目'}的源类目 BSR 属于各自独立节点排名，请按源类目分别解读，严禁跨类目混排。"
            ),
        },
        "hot_samples": hot_samples,
        "top_brands": top_brands,
        "keyword_cloud": extract_keyword_cloud(products),
        "data_quality": {
            "checks": quality_checks,
            "clean_asin_count": sum(1 for p in products if p["anomaly_count"] == 0),
            "flagged_asin_count": sum(1 for p in products if p["anomaly_count"] > 0),
        },
        "methodology": METHODOLOGY_NOTES,
    }


def query_products(
    scope: Dict[str, Any],
    *,
    q: str = "",
    anomaly: str = "all",
    price_band: str = "all",
    brand: str = "",
    sort: str = "listing_sales_30d",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """Filter, sort (missing values always last), and paginate deduplicated products."""
    status = scope["status"]
    if status.state != "ready":
        return {
            "db_status": status.to_dict(),
            "ready": False,
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": page_size,
            "total_pages": 0,
        }

    products: List[Dict[str, Any]] = list(scope["products"])
    norm_cat: str = scope["category_filter"]

    # Search by ASIN, folded cluster ASINs, Parent ASIN, title, subtitle, description, brand
    # Supports case-insensitive multi-word phrases (including hyphen/plural/xmas normalization)
    query = q.strip().lower()
    if query:
        import re as _re

        def _norm_phrase_text(text: str) -> str:
            low = _re.sub(r"['’]s\b", "", (text or "").lower())
            words = _re.findall(r"[a-z0-9]+", low)
            return " ".join(CANONICAL_MAP.get(w, w) for w in words)

        norm_query = _norm_phrase_text(query)
        is_multi_word = " " in query

        def _matches_product(p: Dict[str, Any]) -> bool:
            if (
                query in p["asin"].lower()
                or any(query in fa.lower() for fa in (p.get("folded_asins") or []))
                or (p["parent_asin"] and query in p["parent_asin"].lower())
                or query in p["title"].lower()
                or query in (p.get("subtitle") or "").lower()
                or query in (p.get("description") or "").lower()
                or (p["brand"] and query in p["brand"].lower())
            ):
                return True
            if is_multi_word and norm_query:
                haystack = _norm_phrase_text(
                    " ".join(filter(None, [p.get("title"), p.get("subtitle"), p.get("description")]))
                )
                if norm_query in haystack:
                    return True
            return False

        products = [p for p in products if _matches_product(p)]

    # Brand filter
    brand_q = brand.strip().lower()
    if brand_q:
        products = [p for p in products if p["brand"] and p["brand"].lower() == brand_q]

    # Anomaly / quality filter
    if anomaly == "clean":
        products = [p for p in products if p["anomaly_count"] == 0]
    elif anomaly == "any_anomaly":
        products = [p for p in products if p["anomaly_count"] > 0]
    elif anomaly == "cross_category":
        products = [p for p in products if p["is_cross_category"]]
    elif anomaly == "duplicate_listing_cluster":
        products = [p for p in products if (p.get("duplicate_cluster_size") or 1) > 1]
    elif anomaly in (
        "same_parent_asin",
        "invalid_price",
        "missing_rating",
        "missing_source_bsr",
        "missing_image",
        "missing_link",
    ):
        products = [p for p in products if anomaly in p["anomalies"]]

    # Price band filter
    if price_band and price_band != "all":
        products = [
            p for p in products if _match_price_band(p["sales_price_usd"], p["price_valid"], price_band)
        ]

    # Guard against cross-category source BSR sorting (also disabled for unconfigured pending categories)
    sort_warning: Optional[str] = None
    effective_sort = sort
    can_sort_bsr = norm_cat != "all" and not bool(scope.get("category_pending_config", False))
    if effective_sort == "source_category_bsr" and not can_sort_bsr:
        effective_sort = "listing_sales_30d"
        sort_warning = "全部类目或待配置类目视图下不可混合排序源类目 BSR，已回退为按近 30 天估算销量排序。请先选择单个已配置类目。"

    allowed_sorts = {
        "listing_sales_30d",
        "sales_price_usd",
        "rating_stars",
        "ratings_count",
        "main_category_bsr",
        "source_category_bsr",
        "online_date",
        "result_position",
    }
    if effective_sort not in allowed_sorts:
        effective_sort = "listing_sales_30d"

    desc = order.lower() != "asc"

    def extract_sort_val(item: Dict[str, Any]) -> Optional[Any]:
        if effective_sort == "sales_price_usd":
            return item["sales_price_usd"] if item["price_valid"] else None
        if effective_sort == "rating_stars":
            return item["rating_stars"] if item["rating_status"] == "valid" else None
        if effective_sort == "source_category_bsr":
            return item["active_source_bsr"]
        if effective_sort == "result_position":
            if norm_cat != "all":
                return item["active_result_position"]
            positions = [s["result_position"] for s in item["source_categories"]]
            return min(positions) if positions else None
        return item.get(effective_sort)

    # Split present vs missing so missing/invalid values ALWAYS sort last in both asc & desc.
    present = [p for p in products if extract_sort_val(p) is not None]
    missing = [p for p in products if extract_sort_val(p) is None]

    present.sort(key=lambda p: (extract_sort_val(p), p["asin"]), reverse=desc)
    missing.sort(key=lambda p: p["asin"])
    ordered = present + missing

    safe_size = max(5, min(100, int(page_size)))
    total = len(ordered)
    total_pages = max(1, math.ceil(total / safe_size)) if total > 0 else 0
    safe_page = max(1, min(int(page), total_pages or 1))
    start = (safe_page - 1) * safe_size
    page_items = ordered[start : start + safe_size]

    return {
        "db_status": status.to_dict(),
        "ready": True,
        "selected_date": scope["selected_date"],
        "category_filter": norm_cat,
        "can_sort_source_bsr": can_sort_bsr,
        "sort": effective_sort,
        "order": "desc" if desc else "asc",
        "sort_warning": sort_warning,
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "total_pages": total_pages,
        "items": page_items,
    }
