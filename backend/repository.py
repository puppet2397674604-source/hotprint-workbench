"""Data repository: reads Sorftime SQLite snapshots and performs two-tier deduplication.

Tier 1 (asin): Deduplicates cross-category observations by (domain, ASIN).
Tier 2 (listing): Collapses duplicate/orphan variant listings sharing the same brand,
same main image asset ID (excluding Amazon Merch dynamic composited templates),
and high title token similarity (Jaccard >= 0.70), without ever summing sales.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import CATEGORY_BY_NODE, DOMAIN_LABELS, resolve_category
from .db import DatabaseStatus, file_signature, inspect_database, open_readonly

TITLE_JACCARD_THRESHOLD = 0.70


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_image_asset_key(url: Optional[str]) -> Optional[str]:
    """Extract canonical Amazon image asset ID; ignore Merch dynamic composited templates (_CLa|)."""
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text or "_CLa" in text:
        return None
    match = re.search(
        r"/I/([A-Za-z0-9+_%=-]+?)(?:\.[^/]*)?\.(?:jpe?g|png|webp)(?:$|\?)",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def normalize_title_tokens(title: Optional[str]) -> Set[str]:
    """Tokenize title with light singular normalization (e.g. sweatshirts -> sweatshirt)."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    tokens: Set[str] = set()
    for word in words:
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.add(word)
    return tokens


def normalize_text_exact(value: Optional[str]) -> str:
    """Normalize whitespace and case for exact title/description equality checks."""
    return " ".join((value or "").strip().lower().split())


def title_jaccard(tokens_a: Set[str], tokens_b: Set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    return len(tokens_a & tokens_b) / len(union) if union else 0.0


def _extract_raw_products(raw_response_text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Parse raw_response safely without exposing sensitive fields."""
    meta: Dict[str, Any] = {
        "api_code": None,
        "api_message": None,
        "page": None,
        "page_count": None,
        "request_consumed": None,
        "request_left": None,
        "raw_bytes": len(raw_response_text.encode("utf-8")) if raw_response_text else 0,
    }
    if not raw_response_text:
        return meta, []
    try:
        parsed = json.loads(raw_response_text)
    except json.JSONDecodeError:
        return meta, []
    if not isinstance(parsed, dict):
        return meta, []

    meta["api_code"] = _safe_int(parsed.get("Code", parsed.get("code")))
    meta["api_message"] = parsed.get("Message", parsed.get("message"))
    meta["request_consumed"] = _safe_int(
        parsed.get("RequestConsumed", parsed.get("requestConsumed"))
    )
    meta["request_left"] = _safe_int(parsed.get("RequestLeft", parsed.get("requestLeft")))

    data = parsed.get("Data", parsed.get("data"))
    if isinstance(data, dict):
        meta["page"] = _safe_int(data.get("Page", data.get("page")))
        meta["page_count"] = _safe_int(data.get("PageCount", data.get("pageCount")))
        products = data.get("Products", data.get("products"))
        if isinstance(products, list):
            return meta, [p for p in products if isinstance(p, dict)]
    return meta, []


def _extract_extended_fields(raw_item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract safe supplementary attributes from Sorftime raw_response product object."""
    if not raw_item:
        return {
            "photos": [],
            "ebc_photo_count": 0,
            "online_days": None,
            "variation_asin_count": None,
            "is_fba": None,
            "has_aplus": None,
            "has_video": None,
            "has_brand_store": None,
            "list_price_usd": None,
            "coupon_usd": None,
            "subtitle": "",
            "description": "",
            "buybox_seller_address": None,
            "seller_count": None,
            "bsr_categories_raw": [],
        }

    photos_raw = raw_item.get("Photo")
    photos = [
        str(u).strip()
        for u in (photos_raw if isinstance(photos_raw, list) else [])
        if isinstance(u, str) and u.strip().startswith("http")
    ]
    ebc_raw = raw_item.get("EBCPhoto")
    ebc_count = len(ebc_raw) if isinstance(ebc_raw, list) else 0

    list_price_cents = _safe_float(raw_item.get("ListPrice"))
    coupon_cents = _safe_float(raw_item.get("Coupon"))
    bsr_raw = raw_item.get("BsrCategory")
    bsr_categories: List[Dict[str, Any]] = []
    if isinstance(bsr_raw, list):
        for entry in bsr_raw:
            if isinstance(entry, list) and len(entry) >= 3:
                bsr_categories.append(
                    {
                        "name": _clean_str(entry[0]),
                        "node_id": _clean_str(entry[1]),
                        "rank": _safe_int(entry[2]),
                    }
                )

    return {
        "photos": photos,
        "ebc_photo_count": ebc_count,
        "online_days": _safe_int(raw_item.get("OnlineDays")),
        "variation_asin_count": _safe_int(raw_item.get("VariationASINCount")),
        "is_fba": raw_item.get("IsFBA") if isinstance(raw_item.get("IsFBA"), bool) else None,
        "has_aplus": raw_item.get("APlus") if isinstance(raw_item.get("APlus"), bool) else None,
        "has_video": raw_item.get("HasVideo") if isinstance(raw_item.get("HasVideo"), bool) else None,
        "has_brand_store": (
            raw_item.get("HasBrandStore")
            if isinstance(raw_item.get("HasBrandStore"), bool)
            else None
        ),
        "list_price_usd": (
            round(list_price_cents / 100.0, 2)
            if list_price_cents is not None and list_price_cents > 0
            else None
        ),
        "coupon_usd": (
            round(coupon_cents / 100.0, 2)
            if coupon_cents is not None and coupon_cents > 0
            else None
        ),
        "subtitle": _clean_str(raw_item.get("SubTitle")),
        "description": _clean_str(raw_item.get("Description")),
        "buybox_seller_address": _clean_str(raw_item.get("BuyboxSellerAddress")) or None,
        "seller_count": _safe_int(raw_item.get("SellerCount")),
        "bsr_categories_raw": bsr_categories,
    }


def _representative_sort_key(product: Dict[str, Any]) -> Tuple[Any, ...]:
    """Deterministic representative selection within a duplicate listing cluster.

    Prefers real parent-child listings over ParentAsin=Asin orphans, then listings
    with variation counts, then valid price, then highest 30d sales, then best BSR.
    """
    return (
        0 if not product["needs_parent_verification"] else 1,
        0 if (product.get("variation_asin_count") or 0) > 0 else 1,
        -(product.get("variation_asin_count") or 0),
        0 if product["price_valid"] else 1,
        -(product["listing_sales_30d"] if product["listing_sales_30d"] is not None else -1),
        product["main_category_bsr"] if product["main_category_bsr"] is not None else 10**9,
        product["asin"],
    )


def collapse_duplicate_listings(
    asin_products: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Tier-2 cleaning: group same-brand duplicate listings and leaked orphan variants.

    Within the same non-empty normalized brand, two ASINs belong to the same listing cluster if:
      1. Their non-empty product descriptions (`description`) are exactly identical, OR
      2. Their non-empty product titles (`title`) are exactly identical, OR
      3. They share the exact same canonical main image asset key (excluding Merch `_CLa` dynamic
         templates) AND have high title token similarity (Jaccard >= 0.70).

    Never merges across different brands; never sums sales across folded ASINs; preserves full
    traceable member records (including each member's image, title, sales, price, and BSR).
    """
    brand_buckets: Dict[str, List[int]] = defaultdict(list)
    for idx, prod in enumerate(asin_products):
        brand_key = (prod.get("brand") or "").strip().lower()
        if brand_key:
            brand_buckets[brand_key].append(idx)

    parent_uf: Dict[int, int] = {i: i for i in range(len(asin_products))}
    pair_reasons: Dict[Tuple[int, int], Set[str]] = defaultdict(set)

    def find(x: int) -> int:
        while parent_uf[x] != x:
            parent_uf[x] = parent_uf[parent_uf[x]]
            x = parent_uf[x]
        return x

    def union(a: int, b: int, reason: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent_uf[rb] = ra
        key = (min(a, b), max(a, b))
        pair_reasons[key].add(reason)

    for brand_key, indices in brand_buckets.items():
        if len(indices) < 2:
            continue

        # 1. Exact same non-empty Description within brand
        by_desc: Dict[str, List[int]] = defaultdict(list)
        # 2. Exact same non-empty Title within brand
        by_title: Dict[str, List[int]] = defaultdict(list)
        # 3. Same canonical main image asset within brand
        by_img: Dict[str, List[int]] = defaultdict(list)

        for i in indices:
            desc_norm = normalize_text_exact(asin_products[i].get("description"))
            if desc_norm:
                by_desc[desc_norm].append(i)
            title_norm = normalize_text_exact(asin_products[i].get("title"))
            if title_norm:
                by_title[title_norm].append(i)
            img_key = asin_products[i].get("image_asset_key")
            if img_key:
                by_img[img_key].append(i)

        for group in by_desc.values():
            if len(group) > 1:
                first = group[0]
                for other in group[1:]:
                    union(first, other, "商品介绍完全一致")

        for group in by_title.values():
            if len(group) > 1:
                first = group[0]
                for other in group[1:]:
                    union(first, other, "商品标题完全一致")

        for group in by_img.values():
            if len(group) > 1:
                tokens = {i: normalize_title_tokens(asin_products[i].get("title")) for i in group}
                for pos_a, idx_a in enumerate(group):
                    for idx_b in group[pos_a + 1 :]:
                        if title_jaccard(tokens[idx_a], tokens[idx_b]) >= TITLE_JACCARD_THRESHOLD:
                            union(idx_a, idx_b, "同主图且标题高相似")

    components: Dict[int, List[int]] = defaultdict(list)
    for idx in range(len(asin_products)):
        components[find(idx)].append(idx)

    clusters: List[List[int]] = [comp for comp in components.values() if len(comp) > 1]
    clustered_indices: Set[int] = {i for comp in clusters for i in comp}

    collapsed_products: List[Dict[str, Any]] = []
    cluster_summaries: List[Dict[str, Any]] = []
    asin_to_cluster_info: Dict[str, Dict[str, Any]] = {}

    for c_idx, comp_indices in enumerate(
        sorted(clusters, key=lambda c: (-len(c), (asin_products[c[0]].get("brand") or "").lower(), asin_products[c[0]]["asin"])),
        start=1,
    ):
        members = [asin_products[i] for i in comp_indices]
        members_sorted = sorted(members, key=_representative_sort_key)
        rep = dict(members_sorted[0])
        comp_set = set(comp_indices)

        reasons_found: Set[str] = set()
        for (a, b), rset in pair_reasons.items():
            if a in comp_set and b in comp_set:
                reasons_found.update(rset)
        ordered_reasons = [
            r for r in ("商品介绍完全一致", "商品标题完全一致", "同主图且标题高相似") if r in reasons_found
        ]
        reason_text = " / ".join(ordered_reasons) or "同款重复特征一致"
        orphan_count = sum(1 for m in members_sorted if m["needs_parent_verification"])
        distinct_img_count = len({m.get("image_asset_key") or m.get("main_image_url") for m in members_sorted})
        cluster_id = f"dup_{c_idx}_{rep['asin']}"

        match_reason = (
            f"同品牌（{rep['brand']}）· {reason_text}"
            + (f"（覆盖 {distinct_img_count} 张变体图" if distinct_img_count > 1 else "（同主图")
            + (f"，含 {orphan_count}/{len(members_sorted)} 条 ParentAsin=Asin 孤立变体）" if orphan_count else "）")
        )

        member_records = [
            {
                "asin": m["asin"],
                "parent_asin": m["parent_asin"],
                "asin_status": m["asin_status"],
                "needs_parent_verification": m["needs_parent_verification"],
                "is_representative": m["asin"] == rep["asin"],
                "title": m["title"],
                "main_image_url": m["main_image_url"],
                "listing_sales_30d": m["listing_sales_30d"],
                "sales_price_usd": m["sales_price_usd"],
                "price_valid": m["price_valid"],
                "rating_stars": m["rating_stars"],
                "ratings_count": m["ratings_count"],
                "main_category_bsr": m["main_category_bsr"],
                "online_date": m["online_date"],
                "variation_asin_count": m["variation_asin_count"],
                "amazon_url": m["amazon_url"],
                "source_categories": m["source_categories"],
            }
            for m in members_sorted
        ]

        rep_anomalies = list(rep["anomalies"])
        if "duplicate_listing_cluster" not in rep_anomalies:
            rep_anomalies.append("duplicate_listing_cluster")

        rep["anomalies"] = rep_anomalies
        rep["anomaly_count"] = len(rep_anomalies)
        rep["duplicate_cluster_id"] = cluster_id
        rep["duplicate_cluster_size"] = len(members_sorted)
        rep["collapsed_duplicate_count"] = len(members_sorted) - 1
        rep["duplicate_match_reason"] = match_reason
        rep["duplicate_members"] = member_records
        rep["folded_asins"] = [m["asin"] for m in members_sorted[1:]]
        collapsed_products.append(rep)

        summary = {
            "cluster_id": cluster_id,
            "representative_asin": rep["asin"],
            "brand": rep["brand"],
            "image_asset_key": rep.get("image_asset_key"),
            "main_image_url": rep["main_image_url"],
            "title": rep["title"],
            "member_count": len(members_sorted),
            "collapsed_count": len(members_sorted) - 1,
            "orphan_parent_count": orphan_count,
            "match_reason": match_reason,
            "asins": [m["asin"] for m in members_sorted],
        }
        cluster_summaries.append(summary)

        for m in members_sorted:
            asin_to_cluster_info[m["asin"]] = {
                "cluster_id": cluster_id,
                "representative_asin": rep["asin"],
                "is_representative": m["asin"] == rep["asin"],
                "cluster_size": len(members_sorted),
                "match_reason": match_reason,
                "members": member_records,
            }

    for idx, prod in enumerate(asin_products):
        if idx in clustered_indices:
            continue
        item = dict(prod)
        item["duplicate_cluster_id"] = None
        item["duplicate_cluster_size"] = 1
        item["collapsed_duplicate_count"] = 0
        item["duplicate_match_reason"] = None
        item["duplicate_members"] = []
        item["folded_asins"] = []
        collapsed_products.append(item)

    meta = {
        "cluster_count": len(cluster_summaries),
        "clustered_asin_count": sum(c["member_count"] for c in cluster_summaries),
        "collapsed_asin_count": sum(c["collapsed_count"] for c in cluster_summaries),
        "clusters": cluster_summaries,
        "asin_to_cluster": asin_to_cluster_info,
    }
    return collapsed_products, meta


class SnapshotRepository:
    """Thread-safe read-only loader with automatic invalidation on DB mtime/size change."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._cache_sig: Optional[tuple] = None
        self._cache_payload: Optional[Dict[str, Any]] = None

    def status(self) -> DatabaseStatus:
        return inspect_database(self.db_path)

    def _load_snapshot(self) -> Dict[str, Any]:
        status = inspect_database(self.db_path)
        if status.state != "ready":
            return {
                "status": status,
                "batches": [],
                "observations": [],
                "dates": [],
            }

        sig = file_signature(self.db_path)
        with self._lock:
            if self._cache_sig == sig and self._cache_payload is not None:
                return self._cache_payload

        batches: List[Dict[str, Any]] = []
        raw_products_by_batch: Dict[int, List[Dict[str, Any]]] = {}

        with open_readonly(self.db_path) as conn:
            batch_rows = conn.execute(
                """
                SELECT id, observed_date, fetched_at, domain, node_id,
                       product_count, request_consumed, raw_response
                FROM category_sales_batches
                ORDER BY observed_date DESC, id DESC
                """
            ).fetchall()

            for row in batch_rows:
                batch_id = int(row["id"])
                node_id = str(row["node_id"])
                cat_spec = CATEGORY_BY_NODE.get(node_id)
                meta, raw_items = _extract_raw_products(row["raw_response"])
                raw_products_by_batch[batch_id] = raw_items
                batches.append(
                    {
                        "id": batch_id,
                        "observed_date": row["observed_date"],
                        "fetched_at": row["fetched_at"],
                        "domain": int(row["domain"]),
                        "domain_label": DOMAIN_LABELS.get(int(row["domain"]), f"Domain {row['domain']}"),
                        "node_id": node_id,
                        "source_category": cat_spec.name if cat_spec else f"类目 {node_id}",
                        "short_name": cat_spec.short_name if cat_spec else f"类目 {node_id}",
                        "category_key": cat_spec.key if cat_spec else node_id,
                        "garment_type": cat_spec.garment_type if cat_spec else "unknown",
                        "garment_label": cat_spec.garment_label if cat_spec else "未分类",
                        "gender": cat_spec.gender if cat_spec else "unknown",
                        "gender_label": cat_spec.gender_label if cat_spec else "未知",
                        "color": cat_spec.color if cat_spec else "#475569",
                        "soft_color": cat_spec.soft_color if cat_spec else "#F1F5F9",
                        "product_count": int(row["product_count"]),
                        "request_consumed": _safe_int(row["request_consumed"]),
                        "page": meta["page"] or 1,
                        "page_count": meta["page_count"],
                        "api_code": meta["api_code"],
                        "api_message": meta["api_message"],
                        "source": "supabase" if str(meta.get("api_message") or "").strip().lower().startswith("supabase") else "sorftime",
                        "request_left_after": meta["request_left"],
                        "raw_bytes": meta["raw_bytes"],
                        "status": "succeeded" if meta["api_code"] in (0, None) else "error",
                    }
                )

            obs_rows = conn.execute(
                """
                SELECT v.*, b.fetched_at
                FROM category_sales_by_category AS v
                JOIN category_sales_batches AS b ON b.id = v.batch_id
                ORDER BY v.observed_date DESC, v.batch_id ASC, v.result_position ASC
                """
            ).fetchall()

        observations: List[Dict[str, Any]] = []
        for row in obs_rows:
            batch_id = int(row["batch_id"])
            pos = int(row["result_position"])
            raw_list = raw_products_by_batch.get(batch_id, [])
            raw_item = raw_list[pos - 1] if 0 <= pos - 1 < len(raw_list) else None
            ext = _extract_extended_fields(raw_item)

            node_id = str(row["node_id"])
            cat_spec = CATEGORY_BY_NODE.get(node_id)
            source_category = row["source_category"] or (cat_spec.name if cat_spec else f"类目 {node_id}")

            price = _safe_float(row["sales_price_usd"])
            revenue = _safe_float(row["listing_revenue_30d_usd"])
            rating_stars = _safe_float(row["rating_stars"])
            ratings_count = _safe_int(row["ratings_count"])
            sales_30d = _safe_int(row["listing_sales_30d"])
            main_bsr = _safe_int(row["main_category_bsr"])
            src_bsr = _safe_int(row["source_category_bsr"])
            asin = _clean_str(row["asin"])
            parent_asin = _clean_str(row["parent_asin"]) or None
            asin_status = _clean_str(row["asin_status"]) or (
                "父子编号相同，需核对"
                if parent_asin and asin == parent_asin
                else ("未返回父 ASIN" if not parent_asin else "父子编号不同")
            )
            amazon_url = _clean_str(row["amazon_url"]) or None
            main_image_url = _clean_str(row["main_image_url"]) or (
                ext["photos"][0] if ext["photos"] else None
            )

            price_valid = price is not None and price > 0
            price_status = "valid" if price_valid else ("zero" if price == 0 else "missing")
            rating_valid = rating_stars is not None and rating_stars > 0
            rating_status = "valid" if rating_valid else "missing"

            observations.append(
                {
                    "batch_id": batch_id,
                    "observed_date": row["observed_date"],
                    "fetched_at": row["fetched_at"],
                    "domain": int(row["domain"]),
                    "node_id": node_id,
                    "source_category": source_category,
                    "short_name": cat_spec.short_name if cat_spec else source_category,
                    "category_key": cat_spec.key if cat_spec else node_id,
                    "garment_type": cat_spec.garment_type if cat_spec else "unknown",
                    "garment_label": cat_spec.garment_label if cat_spec else "未分类",
                    "gender": cat_spec.gender if cat_spec else "unknown",
                    "gender_label": cat_spec.gender_label if cat_spec else "未知",
                    "color": cat_spec.color if cat_spec else "#475569",
                    "soft_color": cat_spec.soft_color if cat_spec else "#F1F5F9",
                    "result_position": pos,
                    "asin": asin,
                    "parent_asin": parent_asin,
                    "asin_status": asin_status,
                    "needs_parent_verification": bool(parent_asin and asin == parent_asin),
                    "amazon_url": amazon_url,
                    "title": _clean_str(row["title"]),
                    "listing_sales_30d": sales_30d,
                    "rating_stars": round(rating_stars, 2) if rating_valid else None,
                    "rating_status": rating_status,
                    "ratings_count": ratings_count,
                    "sales_price_usd": round(price, 2) if price_valid else (0.0 if price == 0 else None),
                    "price_valid": price_valid,
                    "price_status": price_status,
                    "listing_revenue_30d_usd": (
                        round(revenue, 2) if revenue is not None and revenue > 0 else (0.0 if revenue == 0 else None)
                    ),
                    "brand": _clean_str(row["brand"]) or None,
                    "main_image_url": main_image_url,
                    "image_asset_key": extract_image_asset_key(main_image_url),
                    "online_date": _clean_str(row["online_date"]) or None,
                    "main_category_bsr": main_bsr,
                    "source_category_bsr": src_bsr,
                    **ext,
                }
            )

        dates = sorted({b["observed_date"] for b in batches}, reverse=True)
        payload = {
            "status": status,
            "batches": batches,
            "observations": observations,
            "dates": dates,
        }
        with self._lock:
            self._cache_sig = sig
            self._cache_payload = payload
        return payload

    def get_batches(self) -> Tuple[DatabaseStatus, List[Dict[str, Any]]]:
        snap = self._load_snapshot()
        return snap["status"], list(snap["batches"])

    def get_available_dates(self) -> List[str]:
        return list(self._load_snapshot()["dates"])

    def get_scope_data(
        self,
        observed_date: Optional[str] = None,
        category: Optional[str] = None,
        dedup_mode: str = "listing",
    ) -> Dict[str, Any]:
        """Return observations and deduplicated records for a date, category, and dedup mode."""
        norm_dedup = "asin" if str(dedup_mode).strip().lower() == "asin" else "listing"
        snap = self._load_snapshot()
        status: DatabaseStatus = snap["status"]
        if status.state != "ready":
            return {
                "status": status,
                "selected_date": observed_date,
                "available_dates": [],
                "category_filter": category or "all",
                "dedup_mode": norm_dedup,
                "batches": [],
                "observations": [],
                "asin_products": [],
                "products": [],
                "duplicate_meta": {
                    "cluster_count": 0,
                    "clustered_asin_count": 0,
                    "collapsed_asin_count": 0,
                    "clusters": [],
                    "asin_to_cluster": {},
                },
                "all_date_observations": [],
            }

        available_dates: List[str] = snap["dates"]
        selected_date = observed_date if observed_date in available_dates else available_dates[0]

        date_batches = [b for b in snap["batches"] if b["observed_date"] == selected_date]
        date_obs = [o for o in snap["observations"] if o["observed_date"] == selected_date]

        asin_all_sources: Dict[str, List[Dict[str, Any]]] = {}
        for obs in date_obs:
            asin_all_sources.setdefault(obs["asin"], []).append(
                {
                    "batch_id": obs["batch_id"],
                    "node_id": obs["node_id"],
                    "category_key": obs["category_key"],
                    "source_category": obs["source_category"],
                    "short_name": obs["short_name"],
                    "garment_type": obs["garment_type"],
                    "garment_label": obs["garment_label"],
                    "gender": obs["gender"],
                    "gender_label": obs["gender_label"],
                    "color": obs["color"],
                    "soft_color": obs["soft_color"],
                    "result_position": obs["result_position"],
                    "source_category_bsr": obs["source_category_bsr"],
                    "fetched_at": obs["fetched_at"],
                }
            )

        norm_cat = "all"
        category_pending_config = False
        if category and category != "all":
            spec = resolve_category(category)
            if spec is not None:
                if spec.is_configured and spec.node_id:
                    norm_cat = spec.node_id
                else:
                    norm_cat = spec.key
                    category_pending_config = True
            else:
                norm_cat = str(category).strip()

        if norm_cat == "all":
            scoped_obs = date_obs
            scoped_batches = date_batches
        elif category_pending_config:
            scoped_obs = []
            scoped_batches = []
        else:
            scoped_obs = [o for o in date_obs if o["node_id"] == norm_cat]
            scoped_batches = [b for b in date_batches if b["node_id"] == norm_cat]

        asin_products = self._deduplicate_by_asin(scoped_obs, asin_all_sources, norm_cat)
        collapsed_products, dup_meta = collapse_duplicate_listings(asin_products)

        # In "asin" mode, still annotate cluster metadata so users can inspect duplicates transparently
        if norm_dedup == "asin":
            asin_to_cluster = dup_meta["asin_to_cluster"]
            annotated_asin_products: List[Dict[str, Any]] = []
            for p in asin_products:
                item = dict(p)
                cinfo = asin_to_cluster.get(p["asin"])
                if cinfo:
                    anoms = list(item["anomalies"])
                    if "duplicate_listing_cluster" not in anoms:
                        anoms.append("duplicate_listing_cluster")
                    item["anomalies"] = anoms
                    item["anomaly_count"] = len(anoms)
                    item["duplicate_cluster_id"] = cinfo["cluster_id"]
                    item["duplicate_cluster_size"] = cinfo["cluster_size"]
                    item["collapsed_duplicate_count"] = 0
                    item["duplicate_match_reason"] = cinfo["match_reason"]
                    item["duplicate_members"] = cinfo["members"]
                    item["folded_asins"] = []
                else:
                    item["duplicate_cluster_id"] = None
                    item["duplicate_cluster_size"] = 1
                    item["collapsed_duplicate_count"] = 0
                    item["duplicate_match_reason"] = None
                    item["duplicate_members"] = []
                    item["folded_asins"] = []
                annotated_asin_products.append(item)
            active_products = annotated_asin_products
        else:
            active_products = collapsed_products

        return {
            "status": status,
            "selected_date": selected_date,
            "available_dates": available_dates,
            "category_filter": norm_cat,
            "category_pending_config": category_pending_config,
            "dedup_mode": norm_dedup,
            "batches": scoped_batches,
            "all_date_batches": date_batches,
            "observations": scoped_obs,
            "all_date_observations": date_obs,
            "asin_products": asin_products,
            "products": active_products,
            "duplicate_meta": dup_meta,
        }

    def _deduplicate_by_asin(
        self,
        scoped_obs: List[Dict[str, Any]],
        asin_all_sources: Dict[str, List[Dict[str, Any]]],
        norm_cat: str,
    ) -> List[Dict[str, Any]]:
        """Tier-1 deduplication strictly by (domain, asin); never sums listing sales."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for obs in scoped_obs:
            grouped.setdefault(obs["asin"], []).append(obs)

        products: List[Dict[str, Any]] = []
        for asin, items in grouped.items():
            primary = sorted(
                items,
                key=lambda x: (
                    0 if x["price_valid"] else 1,
                    0 if x["main_image_url"] else 1,
                    x["result_position"],
                    x["batch_id"],
                ),
            )[0]

            all_sources = sorted(
                asin_all_sources.get(asin, []),
                key=lambda s: (s["node_id"], s["result_position"]),
            )
            distinct_nodes = sorted({s["node_id"] for s in all_sources})
            is_cross_category = len(distinct_nodes) > 1

            sales_values = {x["listing_sales_30d"] for x in items if x["listing_sales_30d"] is not None}
            has_sales_conflict = len(sales_values) > 1

            active_source_bsr: Optional[int] = None
            active_result_position: Optional[int] = None
            if norm_cat != "all":
                for src in items:
                    if src["node_id"] == norm_cat:
                        active_source_bsr = src["source_category_bsr"]
                        active_result_position = src["result_position"]
                        break

            anomalies: List[str] = []
            if primary["needs_parent_verification"]:
                anomalies.append("same_parent_asin")
            if not primary["price_valid"]:
                anomalies.append("invalid_price")
            if primary["rating_status"] != "valid":
                anomalies.append("missing_rating")
            if norm_cat != "all":
                if active_source_bsr is None:
                    anomalies.append("missing_source_bsr")
            else:
                if any(s["source_category_bsr"] is None for s in all_sources):
                    anomalies.append("missing_source_bsr")
            if not primary["main_image_url"]:
                anomalies.append("missing_image")
            if not primary["amazon_url"]:
                anomalies.append("missing_link")

            products.append(
                {
                    "asin": asin,
                    "parent_asin": primary["parent_asin"],
                    "asin_status": primary["asin_status"],
                    "needs_parent_verification": primary["needs_parent_verification"],
                    "domain": primary["domain"],
                    "observed_date": primary["observed_date"],
                    "fetched_at": primary["fetched_at"],
                    "amazon_url": primary["amazon_url"],
                    "title": primary["title"],
                    "subtitle": primary["subtitle"],
                    "description": primary["description"],
                    "brand": primary["brand"],
                    "main_image_url": primary["main_image_url"],
                    "image_asset_key": primary["image_asset_key"],
                    "photos": primary["photos"],
                    "ebc_photo_count": primary["ebc_photo_count"],
                    "listing_sales_30d": primary["listing_sales_30d"],
                    "has_sales_conflict": has_sales_conflict,
                    "listing_revenue_30d_usd": primary["listing_revenue_30d_usd"],
                    "sales_price_usd": primary["sales_price_usd"],
                    "price_valid": primary["price_valid"],
                    "price_status": primary["price_status"],
                    "list_price_usd": primary["list_price_usd"],
                    "coupon_usd": primary["coupon_usd"],
                    "rating_stars": primary["rating_stars"],
                    "rating_status": primary["rating_status"],
                    "ratings_count": primary["ratings_count"],
                    "online_date": primary["online_date"],
                    "online_days": primary["online_days"],
                    "main_category_bsr": primary["main_category_bsr"],
                    "active_source_bsr": active_source_bsr,
                    "active_result_position": active_result_position,
                    "variation_asin_count": primary["variation_asin_count"],
                    "is_fba": primary["is_fba"],
                    "has_aplus": primary["has_aplus"],
                    "has_video": primary["has_video"],
                    "has_brand_store": primary["has_brand_store"],
                    "buybox_seller_address": primary["buybox_seller_address"],
                    "seller_count": primary["seller_count"],
                    "bsr_categories_raw": primary["bsr_categories_raw"],
                    "source_categories": all_sources,
                    "category_names": [s["source_category"] for s in all_sources],
                    "garment_types": sorted({s["garment_type"] for s in all_sources if s.get("garment_type") and s["garment_type"] != "unknown"}),
                    "genders": sorted({s["gender"] for s in all_sources if s.get("gender") and s["gender"] != "unknown"}),
                    "is_cross_category": is_cross_category,
                    "cross_category_count": len(distinct_nodes),
                    "anomalies": anomalies,
                    "anomaly_count": len(anomalies),
                    "preprocessing_placeholder": {
                        "visual_print_status": "pending_manual_review",
                        "label": "待人工核图（未启用自动视觉识别）",
                        "excluded_non_print": None,
                    },
                }
            )

        return products

    def get_product_detail(self, asin: str, observed_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Look up any ASIN (including folded members) and attach full duplicate-cluster provenance."""
        scope = self.get_scope_data(observed_date=observed_date, category="all", dedup_mode="asin")
        if scope["status"].state != "ready":
            return None
        target = asin.strip().upper()
        for product in scope["products"]:
            if product["asin"] == target:
                return product
        return None
