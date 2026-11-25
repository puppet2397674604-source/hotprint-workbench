"""Read-only Supabase (`pp_records`) synchronizer for HotPrint Workbench.

Strict engineering red lines:
- ABSOLUTELY READ-ONLY against Supabase: only HTTP `GET` requests are permitted.
  Production `pp_records` uses optimistic concurrency (`revision`); POST/PUT/PATCH/DELETE
  are strictly forbidden and blocked at the HTTP requester layer.
- Uses Python 3 standard library only (`urllib.request`, `json`, `sqlite3`, `re`).
- Credentials (`url`, `api_key`) are persisted ONLY in gitignored `data/supabase_config.local`
  (chmod 0600) and are NEVER returned in plaintext to the browser or written to logs.
- Writes flattened observations ONLY into the local working database
  (`data/category_sales_live.sqlite3`), NEVER touching `../category_sales.sqlite3`
  or the `v0.1.0` backup directory.
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

from .collector import ASIN_PATTERN, bootstrap_live_database, build_live_schema_sql
from .config import (
    CATEGORIES,
    CategorySpec,
    DEFAULT_DB_PATH,
    DEFAULT_DOMAIN,
    DEFAULT_LIVE_DB_PATH,
    DEFAULT_SUPABASE_URL,
    WORKBENCH_SUPABASE_CONFIG_PATH,
    beijing_today,
    serialize_categories,
)

MONTH_MAP: Dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

BRAND_STORE_PREFIX_RE = re.compile(
    r"^(?:visit\s+the\s+|visit\s+)(.+?)(?:\s+store)?$",
    re.IGNORECASE,
)
BRAND_STORE_SUFFIX_RE = re.compile(r"^(.+?)\s+store$", re.IGNORECASE)
BRAND_LABEL_PREFIX_RE = re.compile(r"^brand\s*[:：]\s*(.+)$", re.IGNORECASE)
BSR_DIRTY_SUFFIX_RE = re.compile(
    r"(?:\s*[-|·•/]*\s*\bcustomer\s+reviews?\b\s*[:：]*)+[\s:：]*$",
    re.IGNORECASE,
)
TRAILING_COLON_SPACE_RE = re.compile(r"[\s:：]+$")


def _mask_supabase_key(key: Optional[str]) -> str:
    """Mask Supabase API key locally (zero import dependency on ai_service)."""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    if len(text) <= 8:
        return f"****{text[-4:]}"
    return f"{text[:3]}****{text[-4:]}"


def clean_bsr_category_name(raw_category: Any) -> str:
    """Strip upstream dirty suffixes such as 'Customer Reviews:' and trailing colons/spaces."""
    if raw_category is None:
        return ""
    text = re.sub(r"\s+", " ", str(raw_category).replace("\u200e", "").replace("\u200f", "")).strip()
    if not text:
        return ""
    text = BSR_DIRTY_SUFFIX_RE.sub("", text)
    text = TRAILING_COLON_SPACE_RE.sub("", text)
    return text.strip()


class SupabaseConfigError(ValueError):
    """Raised when Supabase URL or API key configuration is missing or invalid."""


class SupabaseRequestError(RuntimeError):
    """Raised when an upstream Supabase REST GET request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        is_timeout: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout


class SupabaseSyncError(RuntimeError):
    """Raised when flattening or local SQLite ingestion fails."""


def _normalize_category_Lookup_key(text: str) -> str:
    """Normalize category name for exact matching against the 6 target Amazon categories."""
    cleaned = (
        (text or "")
        .strip()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("–", "-")
        .replace("—", "-")
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.lower()


TARGET_CATEGORY_BY_EN_NAME: Dict[str, CategorySpec] = {
    _normalize_category_Lookup_key(c.en_name): c
    for c in CATEGORIES
    if c.is_configured and c.node_id
}
TARGET_CATEGORY_BY_NODE: Dict[str, CategorySpec] = {
    str(c.node_id): c for c in CATEGORIES if c.is_configured and c.node_id
}
TARGET_CATEGORY_BY_KEY: Dict[str, CategorySpec] = {
    c.key: c for c in CATEGORIES if c.is_configured and c.node_id
}


def clean_supabase_brand(raw_brand: Any) -> Optional[str]:
    """Clean Amazon brand strings like 'Visit the Gildan Store' or 'Brand: Foo'."""
    if raw_brand is None:
        return None
    text = re.sub(r"\s+", " ", str(raw_brand).replace("\u200e", "").replace("\u200f", "")).strip()
    if not text:
        return None
    m_label = BRAND_LABEL_PREFIX_RE.match(text)
    if m_label:
        text = m_label.group(1).strip()
    m_visit = BRAND_STORE_PREFIX_RE.match(text)
    if m_visit:
        text = m_visit.group(1).strip()
    else:
        # Also handle "Visit the X" without trailing Store or "X Store" if preceded by Visit
        if text.lower().startswith("visit the "):
            text = text[10:].strip()
            m_suf = BRAND_STORE_SUFFIX_RE.match(text)
            if m_suf:
                text = m_suf.group(1).strip()
    return text or None


def parse_date_first_available(raw_value: Any) -> Optional[str]:
    """Parse Amazon 'Date First Available' strings (e.g. 'March 13, 2026') into 'YYYY-MM-DD'."""
    if raw_value is None:
        return None
    text = re.sub(r"\s+", " ", str(raw_value).replace("\u200e", "").replace("\u200f", "")).strip()
    if not text:
        return None

    # 1. ISO format: YYYY-MM-DD or YYYY/MM/DD
    m_iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return datetime(y, m, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # 2. US format: "March 13, 2026" or "Mar. 13 2026"
    m_us = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", text)
    if m_us:
        mon_num = MONTH_MAP.get(m_us.group(1).lower())
        if mon_num:
            try:
                return datetime(int(m_us.group(3)), mon_num, int(m_us.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                return None

    # 3. Day-first format: "13 March 2026" or "13 Mar, 2026"
    m_eu = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$", text)
    if m_eu:
        mon_num = MONTH_MAP.get(m_eu.group(2).lower())
        if mon_num:
            try:
                return datetime(int(m_eu.group(3)), mon_num, int(m_eu.group(1))).strftime("%Y-%m-%d")
            except ValueError:
                return None

    return None


def _parse_positive_int(raw_val: Any) -> Optional[int]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if raw_val > 0 else None
    if isinstance(raw_val, float):
        if raw_val > 0 and not (raw_val != raw_val):
            return int(round(raw_val))
        return None
    text = str(raw_val).strip().lstrip("#").replace(",", "").strip()
    if re.fullmatch(r"\d+", text):
        val = int(text)
        return val if val > 0 else None
    return None


def _parse_non_negative_int(raw_val: Any) -> Optional[int]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if raw_val >= 0 else None
    if isinstance(raw_val, float):
        if raw_val >= 0 and not (raw_val != raw_val):
            return int(round(raw_val))
        return None
    text = str(raw_val).strip().replace(",", "").strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def _parse_positive_float(raw_val: Any) -> Optional[float]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, (int, float)):
        val = float(raw_val)
        if val > 0 and not (val != val) and val != float("inf"):
            return round(val, 2)
        return None
    text = str(raw_val).strip().lstrip("$").replace(",", "").strip()
    if not text:
        return None
    try:
        val = float(text)
        if val > 0 and not (val != val) and val != float("inf"):
            return round(val, 2)
    except ValueError:
        pass
    return None


def match_target_category(raw_category: Any) -> Optional[CategorySpec]:
    """Match a BSR category string against the 6 target core apparel categories."""
    if not raw_category:
        return None
    raw_str = clean_bsr_category_name(raw_category)
    if not raw_str:
        return None
    if raw_str in TARGET_CATEGORY_BY_NODE:
        return TARGET_CATEGORY_BY_NODE[raw_str]
    if raw_str in TARGET_CATEGORY_BY_KEY:
        return TARGET_CATEGORY_BY_KEY[raw_str]
    norm = _normalize_category_Lookup_key(raw_str)
    if norm in TARGET_CATEGORY_BY_EN_NAME:
        return TARGET_CATEGORY_BY_EN_NAME[norm]
    return None


def extract_bsr_mappings(
    raw_bsr: Any,
) -> Tuple[Optional[int], List[Dict[str, Any]], List[List[Any]], List[str]]:
    """Extract main_category_bsr, matched target categories, Sorftime-compatible BsrCategory triples, and unmatched names."""
    main_category_bsr: Optional[int] = None
    matched_by_node: Dict[str, Tuple[CategorySpec, Optional[int]]] = {}
    triples: List[List[Any]] = []
    unmatched_names: List[str] = []

    if not isinstance(raw_bsr, list):
        return None, [], [], []

    for entry in raw_bsr:
        if not isinstance(entry, dict):
            continue
        cat_name = clean_bsr_category_name(
            entry.get("category")
            or entry.get("name")
            or entry.get("categoryName")
            or entry.get("label")
            or ""
        )
        raw_rank_val = entry.get("rank") if entry.get("rank") is not None else entry.get("value")
        rank = _parse_positive_int(raw_rank_val)
        if not cat_name:
            continue

        norm_cat = _normalize_category_Lookup_key(cat_name)
        if norm_cat in ("clothing, shoes & jewelry", "clothing, shoes &amp; jewelry", "clothing shoes & jewelry"):
            if rank is not None and (main_category_bsr is None or rank < main_category_bsr):
                main_category_bsr = rank
            continue

        spec = match_target_category(cat_name)
        if spec is None and entry.get("nodeId"):
            spec = match_target_category(entry.get("nodeId"))
        if spec is not None and spec.node_id:
            nid = str(spec.node_id)
            prev = matched_by_node.get(nid)
            if prev is None:
                matched_by_node[nid] = (spec, rank)
            else:
                prev_rank = prev[1]
                if rank is not None and (prev_rank is None or rank < prev_rank):
                    matched_by_node[nid] = (spec, rank)
            if rank is not None:
                triples.append([spec.en_name, nid, rank])
        else:
            if cat_name not in unmatched_names:
                unmatched_names.append(cat_name)

    matched_list: List[Dict[str, Any]] = []
    for nid, (spec, rank) in matched_by_node.items():
        matched_list.append(
            {
                "node_id": nid,
                "category_key": spec.key,
                "name": spec.name,
                "short_name": spec.short_name,
                "en_name": spec.en_name,
                "source_category_bsr": rank,
            }
        )

    return main_category_bsr, matched_list, triples, unmatched_names


VIDEO_OR_ICON_URL_MARKERS = (
    "play-button",
    "play-icon",
    "play_button",
    "play_icon",
    "pkdp-play",
    "pkmb-play",
    "video-slate",
    "videoslate",
    "/vse-",
    "vse-vms",
    "default.jobtemplate",
    "360_icon",
    "360-icon",
    "grey-pixel",
    "transparent-pixel",
    "loading-",
    "spinner",
)


def _extract_valid_product_image_url(raw_img: Any) -> Optional[str]:
    """Extract a valid product image URL from a string or object, skipping `.svg` icons and video covers."""
    if raw_img is None or isinstance(raw_img, bool):
        return None
    if isinstance(raw_img, dict):
        if raw_img.get("isVideo") is True or raw_img.get("is_video") is True:
            return None
        media_kind = str(
            raw_img.get("type")
            or raw_img.get("mediaType")
            or raw_img.get("kind")
            or raw_img.get("variant")
            or ""
        ).strip().lower()
        if any(tok in media_kind for tok in ("video", "mp4", "m3u8", "hls", "play", "icon", "sprite")):
            return None
        raw_url = (
            raw_img.get("url")
            or raw_img.get("hiRes")
            or raw_img.get("large")
            or raw_img.get("mainUrl")
            or raw_img.get("src")
            or ""
        )
    elif isinstance(raw_img, str):
        raw_url = raw_img
    else:
        return None

    url = str(raw_url or "").strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        return None

    lower_url = url.lower()
    path_lower = urllib_parse.urlsplit(lower_url).path
    if path_lower.endswith((".svg", ".mp4", ".webm", ".m3u8", ".mov", ".avi")):
        return None
    if ".svg?" in lower_url or "format=svg" in lower_url:
        return None
    if any(marker in lower_url for marker in VIDEO_OR_ICON_URL_MARKERS):
        return None
    return url


def _read_seller_field_value(fields_obj: Any, field_name: str) -> Any:
    """Safely read `seller.fields.<field_name>.value`, returning None if null or `availability == 'masked'`."""
    if not isinstance(fields_obj, dict):
        return None
    entry = fields_obj.get(field_name)
    if entry is None:
        return None
    if isinstance(entry, dict):
        availability = str(entry.get("availability") or "").strip().lower()
        if availability in ("masked", "hidden", "redacted", "unavailable"):
            return None
        val = entry.get("value")
    else:
        val = entry
    if val is None:
        return None
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped or "*" in stripped or stripped.lower() in ("masked", "null", "none", "n/a", "-", "--"):
            return None
    return val


def _parse_record_timestamp_epoch(row: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[int, float]:
    """Parse `capturedAt` / `updated_at` into `(has_timestamp, utc_epoch_seconds)` for descending merge."""
    seller_obj = payload.get("seller") if isinstance(payload.get("seller"), dict) else {}
    candidates = (
        payload.get("capturedAt"),
        payload.get("captured_at"),
        seller_obj.get("capturedAt") if isinstance(seller_obj, dict) else None,
        row.get("updated_at") if isinstance(row, dict) else None,
        payload.get("updatedAt"),
        payload.get("updated_at"),
    )
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val > 1e12:
                val /= 1000.0
            if val > 0:
                return 1, val
            continue
        txt = str(raw).strip()
        if not txt:
            continue
        iso_txt = txt[:-1] + "+00:00" if txt.endswith("Z") else txt
        try:
            dt = datetime.fromisoformat(iso_txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return 1, dt.timestamp()
        except ValueError:
            parsed_day = parse_date_first_available(txt)
            if parsed_day:
                try:
                    dt_day = datetime.fromisoformat(parsed_day).replace(tzinfo=timezone.utc)
                    return 1, dt_day.timestamp()
                except ValueError:
                    pass
    return 0, 0.0


def _unwrap_record_payload(row: Any) -> Dict[str, Any]:
    """Extract `payload` dict from a `pp_records` row (supports nested `payload` dict/JSON or flat dict)."""
    if not isinstance(row, dict):
        return {}
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return row


def flatten_supabase_records(
    snapshots_rows: List[Dict[str, Any]],
    discoveries_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Flatten raw `pp_records` (`snapshots` + `discoveries`) into 6-category observations & diagnostics.

    Merges multiple snapshots of the same ASIN in descending `capturedAt` / `updated_at` order so
    SellerSprite list imports (sales, revenue, launch_date, top-level parentAsin, images[]) and
    product detail scrapes (mainImage, rating, specs, BSR) complement each other cleanly.
    """
    # 1. Index discoveries by ASIN -> newest valid parentAsin
    discovery_entries_by_asin: Dict[str, List[Tuple[int, float, int, str]]] = {}
    valid_discovery_rows = 0
    for idx, row in enumerate(discoveries_rows or []):
        payload = _unwrap_record_payload(row)
        if not payload:
            continue
        valid_discovery_rows += 1
        asin = str(payload.get("asin") or row.get("asin") or row.get("record_id") or "").strip().upper()
        if not ASIN_PATTERN.fullmatch(asin):
            continue
        raw_parent = (
            payload.get("parentAsin")
            or payload.get("parent_asin")
            or ((payload.get("source") or {}).get("parentAsin") if isinstance(payload.get("source"), dict) else None)
        )
        parent = str(raw_parent or "").strip().upper()
        if ASIN_PATTERN.fullmatch(parent):
            has_ts, ts_val = _parse_record_timestamp_epoch(row, payload)
            discovery_entries_by_asin.setdefault(asin, []).append((has_ts, ts_val, idx, parent))

    discovery_parent_by_asin: Dict[str, str] = {}
    for asin, entries in discovery_entries_by_asin.items():
        entries.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        discovery_parent_by_asin[asin] = entries[0][3]

    # 2. Group snapshots by ASIN and sort each ASIN's snapshots newest -> oldest
    snapshots_by_asin: Dict[str, List[Tuple[int, float, int, Dict[str, Any], Dict[str, Any]]]] = {}
    invalid_asin_rows = 0

    for idx, row in enumerate(snapshots_rows or []):
        payload = _unwrap_record_payload(row)
        if not payload:
            continue
        asin = str(payload.get("asin") or row.get("asin") or row.get("record_id") or "").strip().upper()
        if not ASIN_PATTERN.fullmatch(asin):
            invalid_asin_rows += 1
            continue
        has_ts, ts_val = _parse_record_timestamp_epoch(row, payload)
        snapshots_by_asin.setdefault(asin, []).append((has_ts, ts_val, idx, row, payload))

    products_by_asin: Dict[str, Dict[str, Any]] = {}
    multi_snapshot_asins = 0

    for asin, snap_list in snapshots_by_asin.items():
        if len(snap_list) > 1:
            multi_snapshot_asins += 1
        # Sort newest first: valid timestamp desc, then input index desc
        snap_list.sort(key=lambda x: (-x[0], -x[1], -x[2]))

        top_parent_asin: Optional[str] = None
        source_parent_asin: Optional[str] = None
        title: Optional[str] = None
        brand: Optional[str] = None
        sales_price_usd: Optional[float] = None
        rating_stars: Optional[float] = None
        ratings_count: Optional[int] = None
        explicit_main_image: Optional[str] = None
        gallery_photos: List[str] = []
        custom_amazon_url: Optional[str] = None
        seller_launch_date: Optional[str] = None
        specs_online_date: Optional[str] = None
        listing_sales_30d: Optional[int] = None
        listing_revenue_30d_usd: Optional[float] = None
        main_category_bsr: Optional[int] = None
        description: str = ""

        matched_by_node: Dict[str, Dict[str, Any]] = {}
        bsr_triples: List[List[Any]] = []
        seen_triples = set()
        unmatched_cats: List[str] = []

        for _, _, _, _row, payload in snap_list:
            # Parent ASIN tiers: top-level payload.parentAsin vs payload.source.parentAsin
            if top_parent_asin is None:
                cand_top = str(payload.get("parentAsin") or payload.get("parent_asin") or "").strip().upper()
                if ASIN_PATTERN.fullmatch(cand_top):
                    top_parent_asin = cand_top
            if source_parent_asin is None:
                src_obj = payload.get("source")
                raw_src_p = (
                    src_obj.get("parentAsin") or src_obj.get("parent_asin")
                    if isinstance(src_obj, dict)
                    else None
                )
                cand_src = str(raw_src_p or "").strip().upper()
                if ASIN_PATTERN.fullmatch(cand_src):
                    source_parent_asin = cand_src

            if title is None:
                t_cand = str(payload.get("title") or "").strip()
                if t_cand:
                    title = t_cand

            if brand is None:
                b_cand = clean_supabase_brand(payload.get("brand"))
                if b_cand:
                    brand = b_cand

            # SellerSprite nested fields (`payload.seller.fields`)
            seller_obj = payload.get("seller") if isinstance(payload.get("seller"), dict) else {}
            seller_fields = seller_obj.get("fields") if isinstance(seller_obj, dict) else {}

            if listing_sales_30d is None:
                s_parent = _parse_positive_int(_read_seller_field_value(seller_fields, "sales_parent_30d"))
                s_child = _parse_positive_int(_read_seller_field_value(seller_fields, "sales_child_30d"))
                listing_sales_30d = s_parent if s_parent is not None else s_child

            if listing_revenue_30d_usd is None:
                rev_parent = _parse_positive_float(_read_seller_field_value(seller_fields, "revenue"))
                rev_child = _parse_positive_float(_read_seller_field_value(seller_fields, "revenue_child"))
                listing_revenue_30d_usd = rev_parent if rev_parent is not None else rev_child

            if sales_price_usd is None:
                raw_price_obj = payload.get("price")
                p_cand = (
                    _parse_positive_float(raw_price_obj.get("value"))
                    if isinstance(raw_price_obj, dict)
                    else _parse_positive_float(raw_price_obj)
                )
                if p_cand is None:
                    p_cand = _parse_positive_float(_read_seller_field_value(seller_fields, "price"))
                if p_cand is not None:
                    sales_price_usd = p_cand

            if rating_stars is None:
                raw_rating_obj = payload.get("rating")
                r_cand = (
                    _parse_positive_float(raw_rating_obj.get("value"))
                    if isinstance(raw_rating_obj, dict)
                    else _parse_positive_float(raw_rating_obj)
                )
                if r_cand is None:
                    r_cand = _parse_positive_float(_read_seller_field_value(seller_fields, "rating"))
                if r_cand is not None and r_cand <= 5.0:
                    rating_stars = r_cand

            if ratings_count is None:
                raw_rev_obj = payload.get("reviewCount")
                rc_cand = (
                    _parse_non_negative_int(raw_rev_obj.get("value"))
                    if isinstance(raw_rev_obj, dict)
                    else _parse_non_negative_int(raw_rev_obj)
                )
                if rc_cand is None:
                    rc_cand = _parse_non_negative_int(_read_seller_field_value(seller_fields, "ratings_count") or _read_seller_field_value(seller_fields, "reviews"))
                if rc_cand is not None:
                    ratings_count = rc_cand

            # Images: explicit mainImage priority, plus gallery images[] fallback (skipping .svg & video covers)
            cand_main = _extract_valid_product_image_url(
                payload.get("mainImage") or payload.get("main_image_url")
            )
            if explicit_main_image is None and cand_main:
                explicit_main_image = cand_main
            if cand_main and cand_main not in gallery_photos:
                gallery_photos.append(cand_main)

            raw_images = payload.get("images")
            if isinstance(raw_images, list):
                for img_entry in raw_images:
                    valid_u = _extract_valid_product_image_url(img_entry)
                    if valid_u and valid_u not in gallery_photos:
                        gallery_photos.append(valid_u)

            if custom_amazon_url is None:
                raw_url = str(payload.get("url") or "").strip()
                if raw_url.startswith("http"):
                    custom_amazon_url = raw_url

            # Online date tiers: 1) seller.fields.launch_date.value, 2) specs["Date First Available"]
            if seller_launch_date is None:
                raw_launch = _read_seller_field_value(seller_fields, "launch_date")
                parsed_launch = parse_date_first_available(raw_launch)
                if parsed_launch:
                    seller_launch_date = parsed_launch

            if specs_online_date is None:
                specs = payload.get("specs")
                raw_dfa = None
                if isinstance(specs, dict):
                    raw_dfa = specs.get("Date First Available")
                    if raw_dfa is None:
                        for k, v in specs.items():
                            if str(k).strip().lower() == "date first available":
                                raw_dfa = v
                                break
                if raw_dfa is None:
                    raw_dfa = payload.get("dateFirstAvailable") or payload.get("online_date")
                parsed_dfa = parse_date_first_available(raw_dfa)
                if parsed_dfa:
                    specs_online_date = parsed_dfa

            # BSR array mapping: primary `payload.bsr`, fallback `payload.seller.ranks`
            raw_bsr_list = payload.get("bsr")
            if not (isinstance(raw_bsr_list, list) and len(raw_bsr_list) > 0):
                raw_bsr_list = seller_obj.get("ranks") if isinstance(seller_obj, dict) else None
            snap_main_bsr, snap_matched, snap_triples, snap_unmatched = extract_bsr_mappings(raw_bsr_list)

            if main_category_bsr is None and snap_main_bsr is not None:
                main_category_bsr = snap_main_bsr

            for mc in snap_matched:
                nid = mc["node_id"]
                if nid not in matched_by_node:
                    matched_by_node[nid] = dict(mc)
                elif matched_by_node[nid].get("source_category_bsr") is None and mc.get("source_category_bsr") is not None:
                    matched_by_node[nid]["source_category_bsr"] = mc["source_category_bsr"]

            for t in snap_triples:
                tk = (t[0], t[1])
                if tk not in seen_triples:
                    seen_triples.add(tk)
                    bsr_triples.append(t)

            for uc in snap_unmatched:
                if uc not in unmatched_cats:
                    unmatched_cats.append(uc)

            if not description:
                raw_bullets = payload.get("bullets") or payload.get("featureBullets")
                if isinstance(raw_bullets, list):
                    description = " ".join(str(b).strip() for b in raw_bullets if b and str(b).strip())[:3000]
                elif isinstance(payload.get("description"), str):
                    description = payload["description"].strip()[:3000]

        # Finalize tier priorities across all merged snapshots for this ASIN:
        # parent_asin: top-level payload.parentAsin > discoveries.parentAsin > source.parentAsin
        parent_asin = top_parent_asin or discovery_parent_by_asin.get(asin) or source_parent_asin
        # online_date: seller.fields.launch_date > specs["Date First Available"]
        online_date = seller_launch_date or specs_online_date
        # main_image_url: explicit payload.mainImage > first valid image in payload.images[]
        main_image_url = explicit_main_image or (gallery_photos[0] if gallery_photos else None)
        photos: List[str] = []
        if main_image_url:
            photos.append(main_image_url)
        for u in gallery_photos:
            if u not in photos:
                photos.append(u)

        amazon_url = custom_amazon_url or f"https://www.amazon.com/dp/{asin}"
        matched_cats = list(matched_by_node.values())

        products_by_asin[asin] = {
            "asin": asin,
            "parent_asin": parent_asin,
            "title": title,
            "brand": brand,
            "sales_price_usd": sales_price_usd,
            "price_valid": sales_price_usd is not None and sales_price_usd > 0,
            "rating_stars": rating_stars,
            "ratings_count": ratings_count,
            "main_image_url": main_image_url,
            "photos": photos,
            "amazon_url": amazon_url,
            "online_date": online_date,
            "listing_sales_30d": listing_sales_30d,
            "listing_revenue_30d_usd": listing_revenue_30d_usd,
            "main_category_bsr": main_category_bsr,
            "matched_categories": matched_cats,
            "bsr_category_triples": bsr_triples,
            "unmatched_categories": unmatched_cats,
            "description": description,
            "snapshot_merge_count": len(snap_list),
        }

    all_products = list(products_by_asin.values())
    matched_products = [p for p in all_products if p["matched_categories"]]
    unmatched_products = [p for p in all_products if not p["matched_categories"]]

    # Group observations by each of the 6 target categories (node_id)
    observations_by_node: Dict[str, List[Dict[str, Any]]] = {
        str(c.node_id): [] for c in CATEGORIES if c.is_configured and c.node_id
    }
    for prod in matched_products:
        for mc in prod["matched_categories"]:
            nid = mc["node_id"]
            if nid in observations_by_node:
                observations_by_node[nid].append(
                    {
                        **prod,
                        "node_id": nid,
                        "category_key": mc["category_key"],
                        "source_category": mc["name"],
                        "source_category_bsr": mc["source_category_bsr"],
                    }
                )

    # Sort each category's observations by source_category_bsr asc (nulls last), then ASIN asc
    total_observations = 0
    category_breakdown: List[Dict[str, Any]] = []
    for spec in CATEGORIES:
        if not spec.is_configured or not spec.node_id:
            continue
        nid = str(spec.node_id)
        cat_obs = observations_by_node.get(nid, [])
        cat_obs.sort(
            key=lambda x: (
                x["source_category_bsr"] is None or x["source_category_bsr"] <= 0,
                x["source_category_bsr"] if x["source_category_bsr"] is not None else 10**9,
                x["asin"],
            )
        )
        for pos, item in enumerate(cat_obs, start=1):
            item["result_position"] = pos
        total_observations += len(cat_obs)
        category_breakdown.append(
            {
                "key": spec.key,
                "node_id": nid,
                "name": spec.name,
                "short_name": spec.short_name,
                "en_name": spec.en_name,
                "color": spec.color,
                "matched_count": len(cat_obs),
                "valid_price_count": sum(1 for x in cat_obs if x["price_valid"]),
                "valid_bsr_count": sum(
                    1 for x in cat_obs if x["source_category_bsr"] is not None and x["source_category_bsr"] > 0
                ),
            }
        )

    matched_total = len(matched_products)
    missing_price_count = sum(1 for p in matched_products if not p["price_valid"])
    missing_sales_count = sum(1 for p in matched_products if p["listing_sales_30d"] is None)
    valid_sales_count = matched_total - missing_sales_count
    valid_price_count = matched_total - missing_price_count
    valid_image_count = sum(1 for p in matched_products if p.get("main_image_url"))
    missing_image_count = matched_total - valid_image_count
    matched_merged_count = sum(1 for p in matched_products if (p.get("snapshot_merge_count") or 1) > 1)
    unmatched_cat_counter: Dict[str, int] = {}
    for p in unmatched_products:
        for cname in p.get("unmatched_categories") or ["（无 BSR 类目）"]:
            unmatched_cat_counter[cname] = unmatched_cat_counter.get(cname, 0) + 1

    top_unmatched_categories = [
        {"category": k, "count": v}
        for k, v in sorted(unmatched_cat_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]

    return {
        "products_by_asin": products_by_asin,
        "matched_products": matched_products,
        "unmatched_products": unmatched_products,
        "observations_by_node": observations_by_node,
        "category_breakdown": category_breakdown,
        "summary": {
            "snapshot_records_total": len(snapshots_rows or []),
            "discovery_records_total": len(discoveries_rows or []),
            "invalid_asin_rows": invalid_asin_rows,
            "unique_snapshot_asins": len(all_products),
            "merged_multi_snapshot_asin_count": matched_merged_count,
            "matched_asin_count": matched_total,
            "unmatched_asin_count": len(unmatched_products),
            "matched_observation_count": total_observations,
            "matched_category_count": sum(1 for c in category_breakdown if c["matched_count"] > 0),
            "valid_sales_count": valid_sales_count,
            "valid_sales_pct": round((valid_sales_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "missing_sales_count": missing_sales_count,
            "missing_sales_pct": round((missing_sales_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "valid_image_count": valid_image_count,
            "valid_image_pct": round((valid_image_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "missing_image_count": missing_image_count,
            "missing_image_pct": round((missing_image_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "missing_price_count": missing_price_count,
            "missing_price_pct": round((missing_price_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "valid_price_count": valid_price_count,
            "valid_price_pct": round((valid_price_count / matched_total) * 100.0, 1) if matched_total else 0.0,
            "top_unmatched_categories": top_unmatched_categories,
        },
    }


def default_supabase_get_requester(
    url: str,
    headers: Dict[str, str],
    timeout: float = 30.0,
    method: str = "GET",
) -> Tuple[int, Any, Dict[str, str]]:
    """Strictly READ-ONLY (`GET` only) stdlib HTTP client for Supabase PostgREST."""
    if str(method or "GET").upper() != "GET":
        raise PermissionError("安全红线阻断：Supabase 数据源严格只读，禁止发起任何非 GET 请求。")

    req = urllib_request.Request(url, headers=headers, method="GET")
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
                raise SupabaseRequestError(f"Supabase 返回非 JSON 响应（HTTP {status}）。") from exc
            return status, parsed, resp_headers
    except urllib_error.HTTPError as exc:
        code = int(exc.code)
        # HTTP 416 Range Not Satisfiable means pagination reached beyond the end of table
        if code == 416:
            return 416, [], {}
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            err_body = str(exc.reason)
        is_stmt_timeout = "timeout" in err_body.lower() or code == 504
        try:
            err_json = json.loads(err_body)
            if isinstance(err_json, dict):
                msg = err_json.get("message") or err_json.get("error") or err_json.get("hint") or err_body
                raise SupabaseRequestError(
                    f"Supabase 接口返回 HTTP {code}：{msg}",
                    status_code=code,
                    is_timeout=is_stmt_timeout or ("timeout" in str(msg).lower()),
                ) from None
        except json.JSONDecodeError:
            pass
        raise SupabaseRequestError(
            f"Supabase 接口返回 HTTP {code}：{err_body or exc.reason}",
            status_code=code,
            is_timeout=is_stmt_timeout,
        ) from None
    except urllib_error.URLError as exc:
        reason_str = str(exc.reason)
        raise SupabaseRequestError(
            f"无法连接 Supabase 服务：{exc.reason}",
            is_timeout="timed out" in reason_str.lower() or "timeout" in reason_str.lower(),
        ) from exc
    except TimeoutError as exc:
        raise SupabaseRequestError("Supabase 请求超时，请检查网络后重试。", is_timeout=True) from exc
    except OSError as exc:
        err_str = str(exc)
        raise SupabaseRequestError(
            f"Supabase 网络请求异常：{exc}",
            is_timeout="timed out" in err_str.lower() or "timeout" in err_str.lower(),
        ) from exc


def _is_retryable_supabase_timeout(exc: SupabaseRequestError) -> bool:
    """Return True if the request failed due to HTTP 500/504 or statement/network timeout."""
    if exc.status_code in (500, 504) or exc.is_timeout:
        return True
    msg = str(exc).lower()
    return (
        "http 500" in msg
        or "http 504" in msg
        or "statement timeout" in msg
        or "请求超时" in msg
        or "timed out" in msg
    )


def _encode_keyset_gt_filter(last_record_id: str) -> str:
    """Encode `gt.{last_record_id}` safely for PostgREST query string."""
    if any(ch in last_record_id for ch in ',()" '):
        escaped = last_record_id.replace("\\", "\\\\").replace('"', '\\"')
        return urllib_parse.quote(f'gt."{escaped}"', safe=".")
    return f"gt.{urllib_parse.quote(last_record_id, safe='')}"


class SupabaseSyncService:
    """Manages local Supabase credentials, read-only `pp_records` keyset pagination, and SQLite ingestion."""

    SNAPSHOTS_PAGE_SIZE = 150
    DISCOVERIES_PAGE_SIZE = 400
    DEFAULT_PAGE_SIZE = SNAPSHOTS_PAGE_SIZE
    MAX_TIMEOUT_RETRIES = 2
    MAX_PAGES_GUARD = 1000

    def __init__(
        self,
        *,
        live_db_path: Path = DEFAULT_LIVE_DB_PATH,
        config_path: Path = WORKBENCH_SUPABASE_CONFIG_PATH,
        preprocessing_service: Optional[Any] = None,
        http_requester: Optional[Callable[..., Tuple[int, Any, Dict[str, str]]]] = None,
        timeout: float = 30.0,
    ):
        self.live_db_path = Path(live_db_path)
        self.config_path = Path(config_path)
        self.preprocessing = preprocessing_service
        self.http_requester = http_requester or default_supabase_get_requester
        self.timeout = float(timeout)
        self._lock = threading.Lock()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _clean_url(self, raw_url: Optional[str]) -> str:
        cleaned = str(raw_url or "").strip().rstrip("/")
        if not cleaned:
            return DEFAULT_SUPABASE_URL
        if not (cleaned.startswith("https://") or cleaned.startswith("http://")):
            raise SupabaseConfigError("Supabase URL 必须以 https:// 或 http:// 开头。")
        return cleaned

    def _read_raw_config(self) -> Dict[str, str]:
        cfg = {
            "url": DEFAULT_SUPABASE_URL,
            "api_key": "",
            "source": "",
        }
        if self.config_path.is_file():
            try:
                raw = self.config_path.read_text(encoding="utf-8").strip()
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        if parsed.get("url"):
                            cfg["url"] = str(parsed["url"]).strip().rstrip("/")
                        if parsed.get("api_key"):
                            cfg["api_key"] = str(parsed["api_key"]).strip()
                            cfg["source"] = "workbench_local"
            except Exception:
                pass

        if not cfg["api_key"]:
            env_key = (
                os.environ.get("SUPABASE_API_KEY")
                or os.environ.get("SUPABASE_KEY")
                or os.environ.get("SUPABASE_ANON_KEY")
                or ""
            ).strip()
            if env_key:
                cfg["api_key"] = env_key
                cfg["source"] = "env"
        if os.environ.get("SUPABASE_URL") and not self.config_path.is_file():
            cfg["url"] = str(os.environ["SUPABASE_URL"]).strip().rstrip("/")
        return cfg

    def _write_raw_config(self, url: str, api_key: str) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"url": url, "api_key": api_key}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def get_config_status(self) -> Dict[str, Any]:
        """Return masked Supabase configuration status (NEVER exposes plaintext api_key)."""
        cfg = self._read_raw_config()
        has_key = bool(cfg["api_key"])
        return {
            "configured": has_key,
            "has_api_key": has_key,
            "api_key_masked": _mask_supabase_key(cfg["api_key"]) if has_key else None,
            "api_key_source": cfg["source"] or None,
            "url": cfg["url"],
            "default_url": DEFAULT_SUPABASE_URL,
            "read_only_enforced": True,
            "can_clear_local_key": self.config_path.is_file(),
            "config_file_label": "data/supabase_config.local（已忽略提交）",
        }

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise SupabaseConfigError("请求体必须为 JSON 对象。")
        with self._lock:
            cur = self._read_raw_config()
            url = self._clean_url(
                payload["url"] if "url" in payload and payload["url"] is not None else cur["url"]
            )
            api_key = cur["api_key"] if self.config_path.is_file() else ""
            if "api_key" in payload:
                new_key = str(payload.get("api_key") or "").strip()
                if new_key:
                    if len(new_key) < 8 or any(c.isspace() for c in new_key):
                        raise SupabaseConfigError("Supabase API Key 格式无效（长度不得少于 8 位且不能含空格）。")
                    api_key = new_key
            if not api_key:
                raise SupabaseConfigError("请输入有效的 Supabase API Key。")
            self._write_raw_config(url, api_key)
        return {
            "ok": True,
            "saved": True,
            "message": "Supabase 只读数据源配置已保存至本地文件 data/supabase_config.local。",
            "config": self.get_config_status(),
        }

    def clear_api_key(self) -> Dict[str, Any]:
        with self._lock:
            try:
                if self.config_path.is_file():
                    self.config_path.unlink()
            except OSError as exc:
                raise SupabaseConfigError(f"清除本地 Supabase 配置失败：{exc}") from exc
        return {
            "ok": True,
            "cleared": True,
            "message": "已清除本地保存的 Supabase API Key。",
            "config": self.get_config_status(),
        }

    def _resolve_active_credentials(self, overrides: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        cur = self._read_raw_config()
        url = cur["url"]
        api_key = cur["api_key"]
        if isinstance(overrides, dict):
            if overrides.get("url"):
                url = self._clean_url(overrides.get("url"))
            if overrides.get("api_key") and str(overrides.get("api_key")).strip():
                api_key = str(overrides.get("api_key")).strip()
        if not api_key:
            raise SupabaseConfigError("尚未配置 Supabase API Key，请先填写并保存 Key。")
        return url, api_key

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def test_connection(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Verify read-only connectivity by fetching at most 1 record (`limit=1`)."""
        url, api_key = self._resolve_active_credentials(overrides)
        endpoint = f"{url}/rest/v1/pp_records?select=record_id,collection&order=record_id.asc&limit=1"
        headers = self._build_headers(api_key)
        status, data, _ = self.http_requester(endpoint, headers, self.timeout)
        if not (200 <= status < 300):
            raise SupabaseRequestError(f"连通性测试失败（HTTP {status}）。", status_code=status)
        if not isinstance(data, list):
            raise SupabaseRequestError("Supabase 响应结构异常（预期返回 JSON 数组）。")
        sample_col = data[0].get("collection") if (data and isinstance(data[0], dict)) else None
        return {
            "ok": True,
            "url": url,
            "sample_returned": len(data),
            "sample_collection": sample_col,
            "message": f"Supabase 只读连通性测试通过（HTTP {status}，返回 {len(data)} 条样本，严格只读 GET）。",
            "config": self.get_config_status(),
        }

    def _default_page_size_for_collection(self, collection_name: str) -> int:
        if collection_name == "snapshots":
            return self.SNAPSHOTS_PAGE_SIZE
        if collection_name == "discoveries":
            return self.DISCOVERIES_PAGE_SIZE
        return self.DEFAULT_PAGE_SIZE

    def fetch_collection_paginated(
        self,
        collection_name: str,
        *,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        page_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Keyset-paginate `GET {url}/rest/v1/pp_records` by `record_id.asc` (`record_id=gt.{cursor}&limit=N`).

        Avoids slow PostgreSQL deep `OFFSET` scans on large `snapshots` payloads, and automatically
        halves `limit` (up to `MAX_TIMEOUT_RETRIES = 2` retries) on HTTP 500/504 statement timeouts.
        """
        eff_url, eff_key = (
            (self._clean_url(url), api_key)
            if (url and api_key)
            else self._resolve_active_credentials()
        )
        default_limit = self._default_page_size_for_collection(collection_name)
        current_limit = max(1, min(1000, int(page_size if page_size is not None else default_limit)))
        encoded_col = urllib_parse.quote(f"eq.{collection_name}", safe=".")
        base_endpoint = (
            f"{eff_url}/rest/v1/pp_records"
            f"?select=record_id,collection,payload,updated_at&collection={encoded_col}&order=record_id.asc"
        )
        headers = self._build_headers(eff_key)

        all_rows: List[Dict[str, Any]] = []
        last_record_id: Optional[str] = None

        for page_idx in range(self.MAX_PAGES_GUARD):
            retries_used = 0
            data: Any = None
            used_limit = current_limit

            while True:
                used_limit = current_limit
                cursor_clause = (
                    f"&record_id={_encode_keyset_gt_filter(last_record_id)}"
                    if last_record_id is not None
                    else ""
                )
                endpoint = f"{base_endpoint}{cursor_clause}&limit={used_limit}"
                try:
                    status, resp_data, _ = self.http_requester(endpoint, headers, self.timeout)
                    if status in (500, 504):
                        raise SupabaseRequestError(
                            f"拉取 {collection_name} 第 {page_idx + 1} 页超时或服务端错误（HTTP {status}）。",
                            status_code=status,
                            is_timeout=(status == 504),
                        )
                    if status == 416:
                        resp_data = []
                    elif not (200 <= status < 300):
                        raise SupabaseRequestError(
                            f"拉取 {collection_name} 第 {page_idx + 1} 页失败（HTTP {status}）。",
                            status_code=status,
                        )
                    data = resp_data
                    break
                except SupabaseRequestError as exc:
                    if (
                        _is_retryable_supabase_timeout(exc)
                        and retries_used < self.MAX_TIMEOUT_RETRIES
                        and current_limit > 1
                    ):
                        retries_used += 1
                        current_limit = max(1, current_limit // 2)
                        continue
                    raise

            if not isinstance(data, list) or not data:
                break
            valid_page = [r for r in data if isinstance(r, dict)]
            if not valid_page:
                break
            all_rows.extend(valid_page)

            next_cursor = str(valid_page[-1].get("record_id") or "")
            if not next_cursor or next_cursor == last_record_id:
                break
            last_record_id = next_cursor

            if len(data) < used_limit:
                break

        return all_rows

    def sync_to_local_db(
        self,
        *,
        observed_date: Optional[str] = None,
        dry_run: bool = True,
        page_size: Optional[int] = None,
        snapshots_page_size: Optional[int] = None,
        discoveries_page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Pull `snapshots` + `discoveries` from Supabase (read-only) and preview or write to local DB."""
        target_date = str(observed_date or beijing_today()).strip()
        if len(target_date) != 10 or target_date[4] != "-" or target_date[7] != "-":
            raise SupabaseConfigError("观察日期格式必须为 YYYY-MM-DD。")

        url, api_key = self._resolve_active_credentials()
        snap_limit = snapshots_page_size if snapshots_page_size is not None else (
            page_size if page_size is not None else self.SNAPSHOTS_PAGE_SIZE
        )
        disc_limit = discoveries_page_size if discoveries_page_size is not None else (
            page_size if page_size is not None else self.DISCOVERIES_PAGE_SIZE
        )
        snapshots_rows = self.fetch_collection_paginated(
            "snapshots", url=url, api_key=api_key, page_size=snap_limit
        )
        discoveries_rows = self.fetch_collection_paginated(
            "discoveries", url=url, api_key=api_key, page_size=disc_limit
        )

        flattened = flatten_supabase_records(snapshots_rows, discoveries_rows)
        summary = flattened["summary"]
        category_breakdown = flattened["category_breakdown"]
        observations_by_node = flattened["observations_by_node"]

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "source": "supabase",
                "read_only_enforced": True,
                "observed_date": target_date,
                "supabase_url": url,
                "summary": summary,
                "category_breakdown": category_breakdown,
                "message": (
                    f"Supabase 预检完成：云端共拉取 {summary['snapshot_records_total']} 条 snapshots、"
                    f"{summary['discovery_records_total']} 条 discoveries；"
                    f"匹配 6 大核心服饰类目 {summary['matched_asin_count']} 个去重 ASIN"
                    f"（共 {summary['matched_observation_count']} 条类目观察，覆盖 {summary['matched_category_count']} 个类目）。"
                ),
            }

        if summary["matched_observation_count"] == 0:
            raise SupabaseSyncError(
                f"云端共 {summary['unique_snapshot_asins']} 个商品快照，但没有任何商品命中 6 大核心服饰类目，已取消写入。"
            )

        if self.live_db_path.resolve() == DEFAULT_DB_PATH.resolve():
            raise SupabaseSyncError("安全红线阻断：严禁向原始只读备份库 category_sales.sqlite3 写入数据。")

        fetched_at = self._now_iso()
        written_batches: List[Dict[str, Any]] = []

        with self._lock:
            if (
                self.live_db_path.resolve() == DEFAULT_LIVE_DB_PATH.resolve()
                and not self.live_db_path.exists()
            ):
                bootstrap_live_database(DEFAULT_DB_PATH, self.live_db_path)
            self.live_db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.live_db_path, timeout=45)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.executescript(build_live_schema_sql())

                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for spec in CATEGORIES:
                        if not spec.is_configured or not spec.node_id:
                            continue
                        nid = str(spec.node_id)
                        cat_items = observations_by_node.get(nid, [])
                        if not cat_items:
                            continue

                        raw_products = []
                        obs_tuples = []
                        for pos, item in enumerate(cat_items, start=1):
                            price_cents = (
                                int(round(float(item["sales_price_usd"]) * 100))
                                if item["price_valid"] and item["sales_price_usd"] is not None
                                else None
                            )
                            sales_30d = (
                                int(item["listing_sales_30d"])
                                if item.get("listing_sales_30d") is not None and int(item["listing_sales_30d"]) > 0
                                else None
                            )
                            revenue_cents = (
                                int(round(float(item["listing_revenue_30d_usd"]) * 100))
                                if item.get("listing_revenue_30d_usd") is not None and float(item["listing_revenue_30d_usd"]) > 0
                                else None
                            )
                            raw_products.append(
                                {
                                    "Asin": item["asin"],
                                    "ASIN": item["asin"],
                                    "ParentAsin": item["parent_asin"],
                                    "Title": item["title"],
                                    "Brand": item["brand"],
                                    "Photo": item["photos"],
                                    "SalesPrice": price_cents,
                                    "ListingSalesVolumeOfMonth": sales_30d,
                                    "ListingSalesOfMonth": revenue_cents,
                                    "Ratings": item["rating_stars"],
                                    "RatingsCount": item["ratings_count"],
                                    "OnlineDate": item["online_date"],
                                    "Rank": item["main_category_bsr"],
                                    "BsrCategory": item["bsr_category_triples"],
                                    "Description": item.get("description") or "",
                                    "SubTitle": "",
                                }
                            )
                            obs_tuples.append(
                                (
                                    pos,
                                    item["asin"],
                                    item["parent_asin"],
                                    item["title"],
                                    sales_30d,
                                )
                            )

                        raw_response_obj = {
                            "Code": 0,
                            "Message": "supabase",
                            "Source": "supabase",
                            "RequestConsumed": 0,
                            "RequestLeft": None,
                            "Data": {
                                "Page": 1,
                                "PageCount": 1,
                                "Products": raw_products,
                            },
                        }
                        raw_response_str = json.dumps(raw_response_obj, ensure_ascii=False)

                        existing = conn.execute(
                            """SELECT id FROM category_sales_batches
                               WHERE observed_date = ? AND domain = ? AND node_id = ?""",
                            (target_date, DEFAULT_DOMAIN, nid),
                        ).fetchone()

                        if existing is not None:
                            batch_id = int(existing[0])
                            conn.execute(
                                "DELETE FROM category_sales_observations WHERE batch_id = ?",
                                (batch_id,),
                            )
                            conn.execute(
                                """UPDATE category_sales_batches
                                   SET fetched_at = ?, product_count = ?, request_consumed = 0, raw_response = ?
                                   WHERE id = ?""",
                                (fetched_at, len(obs_tuples), raw_response_str, batch_id),
                            )
                        else:
                            cur = conn.execute(
                                """INSERT INTO category_sales_batches
                                   (observed_date, fetched_at, domain, node_id, product_count, request_consumed, raw_response)
                                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                                (
                                    target_date,
                                    fetched_at,
                                    DEFAULT_DOMAIN,
                                    nid,
                                    len(obs_tuples),
                                    raw_response_str,
                                ),
                            )
                            batch_id = int(cur.lastrowid)

                        conn.executemany(
                            """INSERT INTO category_sales_observations
                               (batch_id, result_position, asin, parent_asin, title, listing_sales_30d)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            [
                                (batch_id, pos, asin, parent, title, sales)
                                for pos, asin, parent, title, sales in obs_tuples
                            ],
                        )

                        written_batches.append(
                            {
                                "batch_id": batch_id,
                                "node_id": nid,
                                "category_key": spec.key,
                                "name": spec.name,
                                "product_count": len(obs_tuples),
                                "credits_used": 0,
                                "source": "supabase",
                            }
                        )

        prep_run = None
        if self.preprocessing is not None:
            try:
                prep_run = self.preprocessing.run_preprocessing(observed_date=target_date)
            except Exception:
                prep_run = None

        return {
            "ok": True,
            "dry_run": False,
            "source": "supabase",
            "read_only_enforced": True,
            "observed_date": target_date,
            "fetched_at": fetched_at,
            "database_modified": len(written_batches) > 0,
            "written_batch_count": len(written_batches),
            "written_batches": written_batches,
            "summary": summary,
            "category_breakdown": category_breakdown,
            "preprocessing_run": prep_run,
            "message": (
                f"已从 Supabase 只读同步写入 {len(written_batches)} 个类目批次（共 {summary['matched_observation_count']} 条观察，"
                f"{summary['matched_asin_count']} 个去重 ASIN，0 额度消耗），观察日期：{target_date}。"
            ),
        }
