"""Stage 03 Data Preprocessing Engine — Plan A: Rule Pre-Screening + Human-Confirmed Exclusion.

Core invariants:
- Original Sorftime SQLite is accessed strictly read-only.
- Automatic text rules NEVER auto-exclude products; suspected non-print & conflicting items
  are routed to `needs_review` for human confirmation.
- Human review decisions (`confirmed_keep`, `confirmed_exclude`) and full audit history are
  persisted in an independent local SQLite database (`data/preprocessing.sqlite3`).
- Re-running preprocessing is idempotent, preserves human decisions & history, and flags
  `review_stale` if a product's title, main image, or parent ASIN changes in a newer snapshot.
- Provides a standardized downstream scoring input contract (`get_scoring_candidates`) that
  strictly defaults to `eligibility=ready` without fabricating scores or filling missing values.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .ai_service import (
    AI_SUGGEST_EXCLUDE_CONFIDENCE,
    PATTERN_TYPE_LABELS,
    compute_evidence_hash,
)
from .config import (
    CATEGORIES,
    CATEGORY_BY_NODE,
    MOTIF_LABELS,
    PRICE_ANOMALY_THRESHOLD_USD,
    SALES_BSR_MIN_SAMPLE_SIZE,
    SALES_BSR_RESIDUAL_SIGMA,
    SEASON_TAG_LABELS,
    TRANSFERABILITY_LABELS,
    VISION_PROMPT_VERSION,
    WORKBENCH_ROOT,
    resolve_category,
    serialize_categories,
)
from .repository import SnapshotRepository

RULE_VERSION = "prep-rules-v0.4-credibility-anomalies"
GENERAL_BASIC_NODE_IDS = {"1044544", "1045624"}
DEFAULT_APP_DB_PATH = WORKBENCH_ROOT / "data" / "preprocessing.sqlite3"

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MANUAL_EXCLUDE_REASONS: Dict[str, str] = {
    "solid_non_print": "纯色 / 无印花",
    "plain_stripe": "普通条纹",
    "non_target_garment": "非目标服装",
    "duplicate_design_manual": "重复设计（人工判断）",
    "other": "其他",
}

MANUAL_KEEP_REASONS: Dict[str, str] = {
    "confirmed_print_visual": "人工核图确认含目标印花",
    "confirmed_embroidery_visual": "人工核图确认含刺绣/贴布图案",
    "keep_for_scoring": "人工确认符合目标范围",
    "other": "其他",
}

AUTO_CLASS_LABELS: Dict[str, str] = {
    "possible_print": "有印花文字线索",
    "suspected_non_print": "疑似非印花",
    "uncertain": "信息不足",
    "conflicting": "规则冲突",
}

MANUAL_STATUS_LABELS: Dict[str, str] = {
    "unreviewed": "未审核",
    "confirmed_keep": "已确认保留",
    "confirmed_exclude": "已确认排除",
}

ELIGIBILITY_LABELS: Dict[str, str] = {
    "ready": "可进入评分",
    "needs_review": "待人工核实",
    "excluded": "已人工排除",
    "blocked": "关键数据阻断",
}

ISSUE_LABELS: Dict[str, str] = {
    "invalid_asin": "ASIN 无效",
    "title_missing": "标题缺失",
    "parent_asin_same": "父子编号相同",
    "parent_asin_missing": "父 ASIN 缺失",
    "price_zero": "售价为 $0",
    "price_missing": "售价缺失",
    "price_anomaly": f"售价异常(<${PRICE_ANOMALY_THRESHOLD_USD:g})",
    "sales_suspect": "销量存疑(BSR背离)",
    "rating_missing": "星级缺失",
    "image_missing": "主图缺失",
    "bsr_missing": "源类目 BSR 缺失",
    "sales_missing": "估算销量缺失",
    "online_date_invalid": "上架日期无效",
    "source_conflict": "跨类目指标冲突",
}

APP_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS preprocessing_runs (
    run_id TEXT PRIMARY KEY,
    observed_date TEXT NOT NULL,
    domain INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    total_observations INTEGER NOT NULL,
    unique_asin_count INTEGER NOT NULL,
    ready_count INTEGER NOT NULL,
    needs_review_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL,
    blocked_count INTEGER NOT NULL,
    suspected_non_print_count INTEGER NOT NULL,
    stale_review_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preprocessed_products (
    observed_date TEXT NOT NULL,
    domain INTEGER NOT NULL,
    asin TEXT NOT NULL,
    parent_asin TEXT,
    title TEXT,
    brand TEXT,
    image_url TEXT,
    photos_json TEXT NOT NULL,
    listing_sales_30d INTEGER,
    sales_price_usd REAL,
    price_valid INTEGER NOT NULL,
    rating_stars REAL,
    ratings_count INTEGER,
    online_date TEXT,
    main_category_bsr INTEGER,
    amazon_url TEXT,
    source_categories_json TEXT NOT NULL,
    source_observations_json TEXT NOT NULL,
    category_bsr_by_source_json TEXT NOT NULL,
    data_quality_issues_json TEXT NOT NULL,
    automatic_classification TEXT NOT NULL,
    automatic_reasons_json TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    observed_at TEXT,
    rule_version TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (observed_date, domain, asin)
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    domain INTEGER NOT NULL,
    asin TEXT NOT NULL,
    review_status TEXT NOT NULL,
    reason_code TEXT,
    reason_note TEXT,
    operator_source TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    last_request_id TEXT,
    PRIMARY KEY (domain, asin)
);

CREATE TABLE IF NOT EXISTS manual_review_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain INTEGER NOT NULL,
    asin TEXT NOT NULL,
    action TEXT NOT NULL,
    prev_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason_code TEXT,
    reason_note TEXT,
    operator_source TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    request_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);
"""


class PreprocessingValidationError(ValueError):
    """Raised on invalid preprocessing or manual review parameters."""


class PreprocessingNotFoundError(LookupError):
    """Raised when target ASIN does not exist in preprocessed snapshot."""


def compute_basis_fingerprint(title: Optional[str], image_url: Optional[str], parent_asin: Optional[str]) -> str:
    """Hash key visual/identity basis fields to detect changes that require human re-review."""
    payload = "|".join(
        [
            " ".join((title or "").strip().lower().split()),
            (image_url or "").strip(),
            (parent_asin or "").strip().upper(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def classify_product_text(
    title: Optional[str],
    subtitle: Optional[str] = "",
    description: Optional[str] = "",
) -> Tuple[str, List[Dict[str, str]]]:
    """Conservative text-based pre-screening rules.

    NEVER concludes visual verification and NEVER auto-excludes.
    - Missing 'graphic' keyword alone yields `uncertain`, NOT `suspected_non_print`.
    - Co-occurring positive print cues and non-print cues yield `conflicting`.
    """
    t = (title or "").strip()
    body = " ".join(filter(None, [(subtitle or "").strip(), (description or "").strip()]))[:6000]

    positive_hits: List[Dict[str, str]] = []
    non_print_hits: List[Dict[str, str]] = []

    def check(target_list: List[Dict[str, str]], code: str, label: str, pattern: str, text: str, field: str) -> None:
        if not text:
            return
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            target_list.append(
                {
                    "code": code,
                    "label": label,
                    "field": field,
                    "quote": m.group(0).strip()[:120],
                }
            )

    # Positive text clues covering both T-Shirt and Sweatshirt (title first, then explicit body descriptions)
    check(
        positive_hits,
        "title_graphic_print",
        "标题含图文印花/文字设计词",
        r"\b(?:graphic(?:s)?(?:\s+(?:tee|t-?shirt|shirt|sweatshirt|top))?|print(?:ed|s)?|letter(?:ing|s)?|slogan(?:s)?|quote[sd]?|typography|meme|band\s+tee|vintage\s+tee|retro\s+(?:tee|t-?shirt|sweatshirt|graphic))\b",
        t,
        "标题",
    )
    check(
        positive_hits,
        "title_embroidery_craft",
        "标题含刺绣/亮片/贴布工艺词",
        r"\b(?:embroider(?:y|ed)|chenille|sequin(?:s|ed)?|appliqu[eé]|patch(?:es)?)\b",
        t,
        "标题",
    )
    check(
        positive_hits,
        "title_novelty_motif",
        "标题含明确主题图案/趣味文案线索",
        r"\b(?:ugly\s+christmas|funny|sarcastic|hilarious|humorous|merry|jolly|spooky|gobble|thankful|ghost|pumpkin|santa|turkey|skeleton|snoopy|peanuts|grinch|witch|skull|frankenstein|hairy\s+chest|american\s+flag|cat\s+print|leopard\s+print|floral\s+print|sunflower\s+print|daisy\s+print|butterfly\s+print|dog\s+mom|cat\s+mom|mama\s+bear|dad\s+joke|retired|coffee\s+lover|book\s+lover|superman|batman|care\s+bears|classic\s+logo|vintage\s+logo)\b",
        t,
        "标题",
    )
    if not positive_hits:
        check(
            positive_hits,
            "body_explicit_print",
            "描述明确提及印花或刺绣工艺",
            r"\b(?:graphic\s+(?:print|design|sweatshirt|tee|t-?shirt|shirt)|printed\s+(?:graphic|design|letter|pattern|tee|t-?shirt)|screen[ -]?print(?:ed)?|digital\s+print|dtg\s+print(?:ed)?|chest\s+print|back\s+print|funny\s+(?:saying|quote|slogan|tee|t-?shirt|sweatshirt)|embroider(?:ed|y)\s+(?:design|letter|pattern|patch|graphic|tee|t-?shirt)|chenille\s+(?:letter|patch))\b",
            body,
            "商品描述",
        )

    # Suspected non-print clues for T-Shirt & Sweatshirt (strictly flagged for human review, never auto-excluded)
    check(
        non_print_hits,
        "suspected_solid_plain",
        "疑似纯色/基础款/多件装素衫（含 plain/solid/basic/pack/fit 等词）",
        r"\b(?:plain|solid(?:\s+color)?|basic(?:\s+(?:tops?|tees?|t-?shirts?|shirts?|sweatshirts?))?|essential(?:\s+(?:tees?|t-?shirts?|shirts?|sweatshirts?))?|amazon\s+essentials|regular[- ]?fit|slim[- ]?fit|athletic[- ]?fit|classic[- ]?fit|relaxed[- ]?fit|double[- ]?lined|buttery\s+soft|tagless|soft\s+cotton\s+crew\s*neck|heavyweight\s+cotton|unprinted|no\s+(?:graphics?|logos?|prints?)|without\s+(?:graphics?|prints?|embroidery)|multi[- ]?pack|\d+[- ]?pack(?:\s+(?:of\s+)?(?:plain|solid|basic|crewneck|v-?neck|cotton|classic|men'?s|women'?s)?\s*(?:tees?|t-?shirts?|shirts?|undershirts?|sweatshirts?|tops?))?)\b",
        t,
        "标题",
    )
    check(
        non_print_hits,
        "suspected_fashion_basic_cut",
        "疑似纯色版型/通勤/运动基础款（修身/褶皱/露肩/不对称/打底/健身等）",
        r"\b(?:fitted\s+(?:short|long|sleeve|crop|workout|t-?shirts?|tees?|tops?|shirts?|clothes)|workout\s+(?:tops?|shirts?|tees?|crop)|gym\s+(?:tops?|shirts?|tees?)|yoga\s+(?:tops?|shirts?)|going\s+out\s+(?:tops?|shirts?)|business\s+casual|dressy\s+casual|work\s+(?:tops?|blouses?|shirts?)|off[- ]?(?:the[- ]?)?shoulder|asymmetrical|ruched|pleated|square\s+neck|scoop\s+neck|boat\s+neck|crop\s+tops?|henley|tunic\s+tops?|modal\s+ribbed|cap\s+sleeve|3/4\s+(?:length\s+)?sleeve)\b",
        t,
        "标题",
    )
    check(
        non_print_hits,
        "suspected_blank_garment",
        "疑似空白胚衣/待加工底衫",
        r"\b(?:blank(?:\s+(?:sweatshirt|shirt|t-?shirt|tee|hoodie|pullover|apparel))?|for\s+(?:screen[ -]?print(?:ing)?|printing|embroidery|sublimation|vinyl|htv|heat\s+transfer)|custom\s+(?:sweatshirt|t-?shirt|tee|shirt)\s+with\s+your\s+(?:image|photo|logo|text|design)|design\s+your\s+own)\b",
        t,
        "标题",
    )
    check(
        non_print_hits,
        "suspected_plain_stripe",
        "疑似普通条纹/色块/肌理基础款",
        r"\b(?:striped?|color[ -]?block(?:ed|ing)?|waffle\s+knit|ribbed\s+(?:fitted|knit|basic|crewneck|t-?shirt|top))\b",
        t,
        "标题",
    )
    check(
        non_print_hits,
        "suspected_non_target",
        "疑似非目标服装类型（儿童/宠物/打底内衣/运动功能素衫）",
        r"\b(?:toddler|infant|newborn|undershirts?|base\s+layer|compression\s+(?:shirt|top|tee|t-?shirt)|moisture\s+wicking|dri[- ]?power|shapewear|bodysuit|sheer|mesh)\b|\b(?:clothes|clothing|apparel|sweatshirt|t-?shirt|shirt)\s+for\s+(?:dogs?|cats?|pets?)\b",
        t,
        "标题",
    )

    if positive_hits and non_print_hits:
        return "conflicting", positive_hits + non_print_hits
    if non_print_hits:
        return "suspected_non_print", non_print_hits
    if positive_hits:
        return "possible_print", positive_hits
    return "uncertain", [
        {
            "code": "insufficient_text_evidence",
            "label": "仅凭当前文字信息不足，无法可靠判断是否含印花（未核图）",
            "field": "标题 / 描述",
            "quote": t[:100] if t else "（标题为空）",
        }
    ]


def is_price_anomaly(
    price_usd: Any,
    threshold_usd: float = PRICE_ANOMALY_THRESHOLD_USD,
) -> bool:
    """Return True iff price is strictly positive yet below the anomaly threshold (e.g. 0 < p < $5)."""
    if price_usd is None:
        return False
    try:
        val = float(price_usd)
    except (TypeError, ValueError):
        return False
    if math.isnan(val) or math.isinf(val):
        return False
    return 0.0 < val < float(threshold_usd)


def detect_sales_bsr_anomalies(
    items: List[Dict[str, Any]],
    *,
    sigma_threshold: float = SALES_BSR_RESIDUAL_SIGMA,
    min_sample_size: int = SALES_BSR_MIN_SAMPLE_SIZE,
) -> Dict[str, Any]:
    """Detect BSR-vs-Sales outliers strictly per single source category (`node_id`) via pure-Python OLS.

    Fits y = ln(listing_sales_30d) against x = ln(source_category_bsr) inside each individual `node_id`
    (NEVER mixing across categories). Categories with fewer than `min_sample_size` valid samples are
    safely skipped with an explicit reason in metadata. If an ASIN spans multiple categories and triggers
    |residual| > sigma_threshold * sigma in ANY category, it is marked `sales_suspect`.
    """
    by_node: Dict[str, Dict[str, Tuple[float, float, str]]] = {}
    node_names: Dict[str, str] = {}

    for item in items or []:
        asin = str(item.get("asin") or "").strip().upper()
        if not asin:
            continue
        raw_sales = item.get("listing_sales_30d")
        if raw_sales is None:
            continue
        try:
            sales = float(raw_sales)
        except (TypeError, ValueError):
            continue
        if math.isnan(sales) or math.isinf(sales) or sales <= 0:
            continue

        bsr_map = item.get("category_bsr_by_source") or {}
        for src in item.get("source_categories") or []:
            nid = str(src.get("node_id") or "").strip()
            if not nid:
                continue
            raw_bsr = src.get("source_category_bsr")
            if raw_bsr is None and isinstance(bsr_map, dict) and nid in bsr_map:
                entry = bsr_map[nid]
                raw_bsr = entry.get("bsr") if isinstance(entry, dict) else entry
            if raw_bsr is None:
                continue
            try:
                bsr = float(raw_bsr)
            except (TypeError, ValueError):
                continue
            if math.isnan(bsr) or math.isinf(bsr) or bsr <= 0:
                continue

            spec = CATEGORY_BY_NODE.get(nid)
            cat_name = (
                spec.short_name
                if spec
                else str(src.get("short_name") or src.get("source_category") or nid)
            )
            node_names[nid] = cat_name
            node_bucket = by_node.setdefault(nid, {})
            if asin not in node_bucket:
                node_bucket[asin] = (bsr, sales, cat_name)

    suspect_map: Dict[str, List[Dict[str, Any]]] = {}
    category_stats: Dict[str, Dict[str, Any]] = {}

    for nid in sorted(by_node.keys()):
        samples = list(by_node[nid].items())
        n = len(samples)
        cat_name = node_names.get(nid, nid)
        if n < int(min_sample_size):
            category_stats[nid] = {
                "node_id": nid,
                "category_name": cat_name,
                "status": "skipped",
                "sample_size": n,
                "min_sample_size": int(min_sample_size),
                "suspect_count": 0,
                "reason": f"有效样本数 {n} < {int(min_sample_size)}，跳过该类目 BSR-销量回归校验",
            }
            continue

        xs = [math.log(bsr) for _, (bsr, _, _) in samples]
        ys = [math.log(sales) for _, (_, sales, _) in samples]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        s_xx = sum((x - mean_x) ** 2 for x in xs)
        s_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = (s_xy / s_xx) if s_xx > 1e-12 else 0.0
        intercept = mean_y - slope * mean_x

        preds = [intercept + slope * x for x in xs]
        residuals = [y - p for y, p in zip(ys, preds)]
        sigma = math.sqrt(sum(r * r for r in residuals) / n)

        if sigma <= 1e-12:
            category_stats[nid] = {
                "node_id": nid,
                "category_name": cat_name,
                "status": "evaluated",
                "sample_size": n,
                "slope": round(slope, 4),
                "intercept": round(intercept, 4),
                "residual_sigma": 0.0,
                "suspect_count": 0,
                "reason": "残差标准差为 0，无离群样本",
            }
            continue

        threshold = float(sigma_threshold) * sigma
        cat_suspects = 0
        for (asin, (bsr, sales, cname)), pred_y, resid in zip(samples, preds, residuals):
            if abs(resid) > threshold:
                cat_suspects += 1
                z_score = resid / sigma
                suspect_map.setdefault(asin, []).append(
                    {
                        "node_id": nid,
                        "category_name": cname,
                        "bsr": int(round(bsr)),
                        "listing_sales_30d": int(round(sales)),
                        "predicted_sales_30d": int(round(math.exp(pred_y))) if pred_y < 25 else None,
                        "residual": round(resid, 3),
                        "z_score": round(z_score, 2),
                        "sigma_threshold": float(sigma_threshold),
                        "direction": "high_sales_vs_bsr" if resid > 0 else "low_sales_vs_bsr",
                    }
                )

        category_stats[nid] = {
            "node_id": nid,
            "category_name": cat_name,
            "status": "evaluated",
            "sample_size": n,
            "slope": round(slope, 4),
            "intercept": round(intercept, 4),
            "residual_sigma": round(sigma, 4),
            "sigma_threshold": float(sigma_threshold),
            "suspect_count": cat_suspects,
            "reason": None,
        }

    return {
        "suspect_map": suspect_map,
        "categories": category_stats,
        "sigma_threshold": float(sigma_threshold),
        "min_sample_size": int(min_sample_size),
        "suspect_asin_count": len(suspect_map),
    }


def check_data_quality_issues(product: Dict[str, Any]) -> List[str]:
    """Inspect independent data quality issues without fabricating or zero-filling missing values."""
    issues: List[str] = []
    asin = (product.get("asin") or "").strip().upper()
    if not ASIN_RE.fullmatch(asin):
        issues.append("invalid_asin")

    if not (product.get("title") or "").strip():
        issues.append("title_missing")

    parent_asin = product.get("parent_asin")
    if not parent_asin:
        issues.append("parent_asin_missing")
    elif parent_asin == asin:
        issues.append("parent_asin_same")

    price = product.get("sales_price_usd")
    price_valid = bool(product.get("price_valid"))
    if not price_valid:
        if price == 0:
            issues.append("price_zero")
        else:
            issues.append("price_missing")
    elif is_price_anomaly(price):
        issues.append("price_anomaly")

    if product.get("rating_status") != "valid" or product.get("rating_stars") is None:
        issues.append("rating_missing")

    if not (product.get("main_image_url") or "").strip():
        issues.append("image_missing")

    sources = product.get("source_categories") or []
    if not sources or any(s.get("source_category_bsr") is None for s in sources):
        issues.append("bsr_missing")

    if product.get("listing_sales_30d") is None:
        issues.append("sales_missing")

    online_date = (product.get("online_date") or "").strip()
    if online_date and not DATE_RE.fullmatch(online_date):
        issues.append("online_date_invalid")

    if product.get("has_sales_conflict"):
        issues.append("source_conflict")

    return issues


def derive_eligibility(
    automatic_classification: str,
    issues: List[str],
    manual_review_status: str,
    review_stale: bool = False,
    source_categories: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """Derive final scoring eligibility from auto classification, quality issues, and human review.

    Guarantees:
    - `suspected_non_print` & `conflicting` NEVER become `excluded` automatically.
    - Critical identity/core-input failure yields `blocked`.
    - Stale manual review (basis changed in new snapshot) forces `needs_review` for re-check.
    """
    issue_set = set(issues)
    # Blocking criteria: cannot safely identify product or establish any sales/rank scoring signal
    has_blocking_issue = (
        "invalid_asin" in issue_set
        or "title_missing" in issue_set
        or ("sales_missing" in issue_set and "bsr_missing" in issue_set)
    )

    # Human explicit exclusion takes effect unless basis has changed (stale -> needs_review)
    if manual_review_status == "confirmed_exclude" and not review_stale:
        return "excluded", "人工已确认排除（可随时恢复）"

    if has_blocking_issue:
        return "blocked", "存在关键身份或核心评分输入缺失，暂阻断进入评分"

    if review_stale and manual_review_status in ("confirmed_keep", "confirmed_exclude"):
        return "needs_review", "商品标题/主图或父体已变化，原人工审核结论需复核"

    if manual_review_status == "confirmed_keep":
        return "ready", "人工已确认保留，可进入后续候选评分"

    # Unreviewed items: suspected_non_print and conflicting MUST wait for human confirmation
    if automatic_classification in ("suspected_non_print", "conflicting"):
        return "needs_review", "自动规则提示疑点，需人工确认保留或排除（不会自动排除）"

    if automatic_classification == "possible_print":
        return "ready", "文字有印花线索且无冲突，暂作为未核图候选进入评分"

    # For general non-novelty T-Shirt categories (Women's / Men's T-Shirts), items without any
    # positive print clues are overwhelmingly plain basics -> route to `needs_review` (never auto-excluded).
    if source_categories:
        node_ids = {str(s.get("node_id") or "") for s in source_categories if s.get("node_id")}
        if node_ids and node_ids.issubset(GENERAL_BASIC_NODE_IDS):
            return "needs_review", "来自通用基础 T 恤类目且缺少印花文字线索，归入待人工核实（不会自动排除）"

    return "ready", "信息不足（未核图），按保守策略暂允许进入评分并向下游透传不确定性"


class PreprocessingService:
    """Manages Stage 03 preprocessing runs, human review persistence, and Stage 04 handoff API."""

    def __init__(self, repo: SnapshotRepository, app_db_path: Path = DEFAULT_APP_DB_PATH):
        self.repo = repo
        self.app_db_path = app_db_path
        self._lock = threading.Lock()
        self._init_app_db()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _connect_app_db(self) -> Iterator[sqlite3.Connection]:
        self.app_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.app_db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _init_app_db(self) -> None:
        with self._lock:
            with self._connect_app_db() as conn:
                conn.executescript(APP_SCHEMA_SQL)
                conn.commit()

    def _resolve_date(self, observed_date: Optional[str]) -> Optional[str]:
        dates = self.repo.get_available_dates()
        if not dates:
            return observed_date
        if observed_date and observed_date in dates:
            return observed_date
        return dates[0]

    def _ensure_preprocessed(self, observed_date: Optional[str]) -> Optional[str]:
        """Auto-run preprocessing on first view or when rule version upgrades (preserving human reviews)."""
        target_date = self._resolve_date(observed_date)
        if not target_date:
            return None
        with self._connect_app_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt, MIN(rule_version) AS min_ver FROM preprocessed_products WHERE observed_date = ?",
                (target_date,),
            ).fetchone()
            cnt = int(row["cnt"] or 0)
            min_ver = row["min_ver"]
        if (cnt == 0 or min_ver != RULE_VERSION) and self.repo.status().state == "ready":
            self.run_preprocessing(observed_date=target_date)
        return target_date

    def run_preprocessing(self, observed_date: Optional[str] = None) -> Dict[str, Any]:
        """Execute (or idempotently re-run) Stage 03 preprocessing on read-only raw SQLite snapshot."""
        started_at = self._now_iso()
        run_id = f"prep_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"

        scope = self.repo.get_scope_data(observed_date=observed_date, category="all", dedup_mode="asin")
        status = scope["status"]
        target_date = scope.get("selected_date") or observed_date or ""

        if status.state != "ready":
            finished_at = self._now_iso()
            summary = {
                "run_id": run_id,
                "observed_date": target_date,
                "domain": 1,
                "rule_version": RULE_VERSION,
                "status": "empty",
                "db_status": status.to_dict(),
                "message": status.message,
                "total_observations": 0,
                "unique_asin_count": 0,
                "counts": {"ready": 0, "needs_review": 0, "excluded": 0, "blocked": 0},
                "suspected_non_print_count": 0,
                "stale_review_count": 0,
            }
            with self._lock:
                with self._connect_app_db() as conn:
                    conn.execute(
                        """INSERT INTO preprocessing_runs
                           (run_id, observed_date, domain, rule_version, started_at, finished_at, status,
                            total_observations, unique_asin_count, ready_count, needs_review_count,
                            excluded_count, blocked_count, suspected_non_print_count, stale_review_count, summary_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id, target_date, 1, RULE_VERSION, started_at, finished_at, "empty",
                            0, 0, 0, 0, 0, 0, 0, 0, json.dumps(summary, ensure_ascii=False),
                        ),
                    )
                    conn.commit()
            return summary

        observations = scope["observations"]
        asin_products = scope["products"]
        obs_by_asin: Dict[str, List[Dict[str, Any]]] = {}
        for o in observations:
            obs_by_asin.setdefault(o["asin"], []).append(
                {
                    "batch_id": o["batch_id"],
                    "node_id": o["node_id"],
                    "source_category": o["source_category"],
                    "result_position": o["result_position"],
                    "source_category_bsr": o["source_category_bsr"],
                    "listing_sales_30d": o["listing_sales_30d"],
                    "sales_price_usd": o["sales_price_usd"],
                    "fetched_at": o["fetched_at"],
                }
            )

        with self._lock:
            with self._connect_app_db() as conn:
                # Load existing human reviews (never overwritten!)
                review_rows = conn.execute(
                    "SELECT * FROM manual_reviews WHERE domain = 1"
                ).fetchall()
                reviews_by_asin = {r["asin"]: dict(r) for r in review_rows}

                counts = {"ready": 0, "needs_review": 0, "excluded": 0, "blocked": 0}
                auto_counts = {k: 0 for k in AUTO_CLASS_LABELS}
                stale_count = 0
                now_iso = self._now_iso()

                upserts = []
                for p in asin_products:
                    asin = p["asin"]
                    auto_class, auto_reasons = classify_product_text(
                        p.get("title"),
                        p.get("subtitle"),
                        p.get("description"),
                    )
                    issues = check_data_quality_issues(p)
                    fingerprint = compute_basis_fingerprint(
                        p.get("title"),
                        p.get("main_image_url"),
                        p.get("parent_asin"),
                    )

                    review = reviews_by_asin.get(asin)
                    manual_status = review["review_status"] if review else "unreviewed"
                    review_stale = bool(
                        review
                        and manual_status in ("confirmed_keep", "confirmed_exclude")
                        and review["basis_fingerprint"] != fingerprint
                    )
                    if review_stale:
                        stale_count += 1

                    eligibility, _ = derive_eligibility(
                        auto_class,
                        issues,
                        manual_status,
                        review_stale,
                        source_categories=p.get("source_categories"),
                    )
                    counts[eligibility] += 1
                    auto_counts[auto_class] += 1

                    bsr_by_source = {
                        s["node_id"]: {
                            "source_category": s["source_category"],
                            "bsr": s["source_category_bsr"],
                            "result_position": s["result_position"],
                        }
                        for s in (p.get("source_categories") or [])
                    }

                    upserts.append(
                        (
                            target_date,
                            int(p["domain"]),
                            asin,
                            p.get("parent_asin"),
                            p.get("title"),
                            p.get("brand"),
                            p.get("main_image_url"),
                            json.dumps(p.get("photos") or [], ensure_ascii=False),
                            p.get("listing_sales_30d"),
                            p.get("sales_price_usd"),
                            1 if p.get("price_valid") else 0,
                            p.get("rating_stars"),
                            p.get("ratings_count"),
                            p.get("online_date"),
                            p.get("main_category_bsr"),
                            p.get("amazon_url"),
                            json.dumps(p.get("source_categories") or [], ensure_ascii=False),
                            json.dumps(obs_by_asin.get(asin, []), ensure_ascii=False),
                            json.dumps(bsr_by_source, ensure_ascii=False),
                            json.dumps(issues, ensure_ascii=False),
                            auto_class,
                            json.dumps(auto_reasons, ensure_ascii=False),
                            fingerprint,
                            p.get("fetched_at"),
                            RULE_VERSION,
                            run_id,
                            now_iso,
                        )
                    )

                # Idempotently replace preprocessed rows for this exact (observed_date, domain)
                # while keeping manual_reviews and manual_review_history completely intact.
                conn.execute(
                    "DELETE FROM preprocessed_products WHERE observed_date = ? AND domain = 1",
                    (target_date,),
                )
                conn.executemany(
                    """INSERT INTO preprocessed_products
                       (observed_date, domain, asin, parent_asin, title, brand, image_url, photos_json,
                        listing_sales_30d, sales_price_usd, price_valid, rating_stars, ratings_count,
                        online_date, main_category_bsr, amazon_url, source_categories_json,
                        source_observations_json, category_bsr_by_source_json, data_quality_issues_json,
                        automatic_classification, automatic_reasons_json, basis_fingerprint, observed_at,
                        rule_version, last_run_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    upserts,
                )

                finished_at = self._now_iso()
                summary = {
                    "run_id": run_id,
                    "observed_date": target_date,
                    "domain": 1,
                    "rule_version": RULE_VERSION,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": "succeeded",
                    "total_observations": len(observations),
                    "unique_asin_count": len(asin_products),
                    "counts": counts,
                    "automatic_counts": auto_counts,
                    "suspected_non_print_count": auto_counts["suspected_non_print"],
                    "stale_review_count": stale_count,
                }
                conn.execute(
                    """INSERT INTO preprocessing_runs
                       (run_id, observed_date, domain, rule_version, started_at, finished_at, status,
                        total_observations, unique_asin_count, ready_count, needs_review_count,
                        excluded_count, blocked_count, suspected_non_print_count, stale_review_count, summary_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, target_date, 1, RULE_VERSION, started_at, finished_at, "succeeded",
                        len(observations), len(asin_products),
                        counts["ready"], counts["needs_review"], counts["excluded"], counts["blocked"],
                        auto_counts["suspected_non_print"], stale_count,
                        json.dumps(summary, ensure_ascii=False),
                    ),
                )
                conn.commit()
                return summary

    def _hydrate_rows(self, observed_date: str) -> List[Dict[str, Any]]:
        """Join persisted preprocessed products with live manual reviews and AI cache."""
        with self._connect_app_db() as conn:
            prod_rows = conn.execute(
                """SELECT * FROM preprocessed_products
                   WHERE observed_date = ? AND domain = 1
                   ORDER BY COALESCE(listing_sales_30d, -1) DESC, asin ASC""",
                (observed_date,),
            ).fetchall()
            review_rows = conn.execute(
                "SELECT * FROM manual_reviews WHERE domain = 1"
            ).fetchall()
            has_ai_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'product_ai_cache'"
            ).fetchone()
            ai_rows = (
                conn.execute("SELECT * FROM product_ai_cache").fetchall()
                if has_ai_table
                else []
            )

        reviews_by_asin = {r["asin"]: dict(r) for r in review_rows}
        ai_by_asin = {r["asin"]: dict(r) for r in ai_rows}
        items: List[Dict[str, Any]] = []
        for row in prod_rows:
            asin = row["asin"]
            issues = json.loads(row["data_quality_issues_json"] or "[]")
            auto_class = row["automatic_classification"]
            auto_reasons = json.loads(row["automatic_reasons_json"] or "[]")
            raw_source_cats = json.loads(row["source_categories_json"] or "[]")
            source_cats = []
            for s in raw_source_cats:
                nid = str(s.get("node_id") or "")
                spec = CATEGORY_BY_NODE.get(nid)
                enriched = dict(s)
                if spec:
                    enriched["category_key"] = spec.key
                    enriched["source_category"] = spec.name
                    enriched["short_name"] = spec.short_name
                    enriched["garment_type"] = spec.garment_type
                    enriched["garment_label"] = spec.garment_label
                    enriched["gender"] = spec.gender
                    enriched["gender_label"] = spec.gender_label
                    enriched["color"] = spec.color
                    enriched["soft_color"] = spec.soft_color
                else:
                    enriched.setdefault("short_name", enriched.get("source_category") or nid)
                    enriched.setdefault("garment_type", "unknown")
                    enriched.setdefault("garment_label", "未分类")
                    enriched.setdefault("gender", "unknown")
                    enriched.setdefault("gender_label", "未知")
                    enriched.setdefault("color", "#475569")
                    enriched.setdefault("soft_color", "#F1F5F9")
                source_cats.append(enriched)

            garment_types = sorted({s["garment_type"] for s in source_cats if s.get("garment_type") and s["garment_type"] != "unknown"})
            genders = sorted({s["gender"] for s in source_cats if s.get("gender") and s["gender"] != "unknown"})
            distinct_nodes = {s["node_id"] for s in source_cats if s.get("node_id")}
            source_obs = json.loads(row["source_observations_json"] or "[]")
            bsr_by_source = json.loads(row["category_bsr_by_source_json"] or "{}")
            photos = json.loads(row["photos_json"] or "[]")
            fp = row["basis_fingerprint"]

            review = reviews_by_asin.get(asin)
            manual_status = review["review_status"] if review else "unreviewed"
            review_stale = bool(
                review
                and manual_status in ("confirmed_keep", "confirmed_exclude")
                and review["basis_fingerprint"] != fp
            )
            eligibility, eligibility_reason = derive_eligibility(
                auto_class,
                issues,
                manual_status,
                review_stale,
                source_categories=source_cats,
            )

            reason_code = review["reason_code"] if review else None
            reason_label = None
            if reason_code:
                reason_label = (
                    MANUAL_EXCLUDE_REASONS.get(reason_code)
                    or MANUAL_KEEP_REASONS.get(reason_code)
                    or reason_code
                )

            ai_row = ai_by_asin.get(asin)
            ai_analysis: Optional[Dict[str, Any]] = None
            ai_suggest_exclude = False
            if ai_row:
                ev_hash = compute_evidence_hash(row["title"], row["image_url"])
                if ai_row.get("evidence_hash") == ev_hash:
                    ai_is_print = bool(ai_row["is_graphic_print"])
                    ai_conf = float(ai_row["confidence"])
                    ai_pattern = ai_row["pattern_type"]
                    ai_pv = str(ai_row.get("prompt_version") or "v1").strip() or "v1"
                    ai_season = str(ai_row.get("season_tag") or "").strip()
                    ai_motif = str(ai_row.get("motif") or "").strip()
                    ai_transfer = str(ai_row.get("transferability") or "").strip()
                    ai_suggest_exclude = (not ai_is_print) and (ai_conf >= AI_SUGGEST_EXCLUDE_CONFIDENCE)
                    ai_analysis = {
                        "cached": True,
                        "model": ai_row["model"],
                        "prompt_version": ai_pv,
                        "needs_v2_upgrade": ai_pv != VISION_PROMPT_VERSION,
                        "is_graphic_print": ai_is_print,
                        "pattern_type": ai_pattern,
                        "pattern_label": PATTERN_TYPE_LABELS.get(ai_pattern, ai_pattern),
                        "confidence": ai_conf,
                        "filter_reason": ai_row.get("filter_reason") or "",
                        "print_subject": ai_row.get("print_subject") or "",
                        "slogan_text": ai_row.get("slogan_text") or "",
                        "visual_style": ai_row.get("visual_style") or "",
                        "theme_tags": json.loads(ai_row.get("theme_tags_json") or "[]"),
                        "season_tag": ai_season,
                        "season_label": SEASON_TAG_LABELS.get(ai_season, ai_season) if ai_season else "",
                        "motif": ai_motif,
                        "motif_label": MOTIF_LABELS.get(ai_motif, ai_motif) if ai_motif else "",
                        "transferability": ai_transfer,
                        "transferability_label": (
                            TRANSFERABILITY_LABELS.get(ai_transfer, ai_transfer)
                            if ai_transfer
                            else ""
                        ),
                        "suggest_exclude": ai_suggest_exclude,
                        "updated_at": ai_row.get("updated_at"),
                    }

            items.append(
                {
                    "site": "US" if row["domain"] == 1 else str(row["domain"]),
                    "domain": int(row["domain"]),
                    "observed_date": row["observed_date"],
                    "observed_at": row["observed_at"],
                    "asin": asin,
                    "parent_asin": row["parent_asin"],
                    "needs_parent_verification": bool(row["parent_asin"] and row["parent_asin"] == asin),
                    "title": row["title"] or "",
                    "brand": row["brand"],
                    "image_url": row["image_url"],
                    "photos": photos,
                    "amazon_url": row["amazon_url"],
                    "amazon_url_verified": bool(row["amazon_url"] and row["parent_asin"] != asin),
                    "listing_sales_30d": row["listing_sales_30d"],
                    "price": row["sales_price_usd"] if row["price_valid"] else None,
                    "raw_price_usd": row["sales_price_usd"],
                    "price_valid": bool(row["price_valid"]),
                    "rating_stars": row["rating_stars"],
                    "ratings_count": row["ratings_count"],
                    "online_date": row["online_date"],
                    "main_category_bsr": row["main_category_bsr"],
                    "source_categories": source_cats,
                    "source_observations": source_obs,
                    "category_bsr_by_source": bsr_by_source,
                    "garment_types": garment_types,
                    "genders": genders,
                    "is_cross_category": len(distinct_nodes) > 1,
                    "cross_category_count": len(distinct_nodes),
                    "data_quality_issues": issues,
                    "data_quality_issue_labels": [
                        {"code": code, "label": ISSUE_LABELS.get(code, code)} for code in issues
                    ],
                    "automatic_classification": auto_class,
                    "automatic_classification_label": AUTO_CLASS_LABELS.get(auto_class, auto_class),
                    "automatic_reasons": auto_reasons,
                    "manual_review_status": manual_status,
                    "manual_review_status_label": MANUAL_STATUS_LABELS.get(manual_status, manual_status),
                    "manual_review_reason_code": reason_code,
                    "manual_review_reason": reason_label,
                    "manual_review_note": review["reason_note"] if review else "",
                    "manual_reviewed_at": review["reviewed_at"] if review else None,
                    "manual_operator_source": review["operator_source"] if review else None,
                    "review_stale": review_stale,
                    "eligibility": eligibility,
                    "eligibility_label": ELIGIBILITY_LABELS.get(eligibility, eligibility),
                    "eligibility_reason": eligibility_reason,
                    "visual_verified": bool(ai_analysis),
                    "ai_analysis": ai_analysis,
                    "ai_suggest_exclude": ai_suggest_exclude,
                    "basis_fingerprint": fp,
                    "rule_version": row["rule_version"],
                    "last_run_id": row["last_run_id"],
                }
            )

        # Attach snapshot-level sibling counts + credibility anomaly flags (price_anomaly & per-category sales_suspect)
        bsr_audit = detect_sales_bsr_anomalies(items)
        suspect_map = bsr_audit["suspect_map"]

        brand_counts: Dict[str, int] = {}
        parent_counts: Dict[str, int] = {}
        for item in items:
            bkey = (item.get("brand") or "").strip().lower()
            if bkey:
                brand_counts[bkey] = brand_counts.get(bkey, 0) + 1
            pkey = (item.get("parent_asin") or "").strip().upper()
            if pkey:
                parent_counts[pkey] = parent_counts.get(pkey, 0) + 1
        for item in items:
            bkey = (item.get("brand") or "").strip().lower()
            pkey = (item.get("parent_asin") or "").strip().upper()
            item["brand_count"] = brand_counts.get(bkey, 0) if bkey else 0
            item["parent_count"] = parent_counts.get(pkey, 0) if pkey else 0

            price_anom = ("price_anomaly" in item["data_quality_issues"]) or is_price_anomaly(
                item.get("raw_price_usd")
            )
            suspect_details = suspect_map.get(item["asin"], [])
            sales_susp = bool(suspect_details)
            item["price_anomaly"] = price_anom
            item["sales_suspect"] = sales_susp
            item["sales_suspect_details"] = suspect_details

            if price_anom and "price_anomaly" not in item["data_quality_issues"]:
                item["data_quality_issues"].append("price_anomaly")
                item["data_quality_issue_labels"].append(
                    {"code": "price_anomaly", "label": ISSUE_LABELS["price_anomaly"]}
                )
            if sales_susp and "sales_suspect" not in item["data_quality_issues"]:
                item["data_quality_issues"].append("sales_suspect")
                item["data_quality_issue_labels"].append(
                    {"code": "sales_suspect", "label": ISSUE_LABELS["sales_suspect"]}
                )
        return items

    def get_overview(self, observed_date: Optional[str] = None) -> Dict[str, Any]:
        db_status = self.repo.status()
        available_dates = self.repo.get_available_dates()
        if db_status.state != "ready":
            return {
                "ready": False,
                "db_status": db_status.to_dict(),
                "selected_date": observed_date,
                "available_dates": available_dates,
                "counts": {"total": 0, "ready": 0, "needs_review": 0, "excluded": 0, "blocked": 0},
                "ai_counts": {
                    "cached_count": 0,
                    "suggest_exclude_count": 0,
                    "suggest_exclude_unreviewed_count": 0,
                    "graphic_print_count": 0,
                },
                "latest_run": None,
                "exclude_reasons": MANUAL_EXCLUDE_REASONS,
                "keep_reasons": MANUAL_KEEP_REASONS,
            }

        target_date = self._ensure_preprocessed(observed_date)
        items = self._hydrate_rows(target_date)

        with self._connect_app_db() as conn:
            latest_run_row = conn.execute(
                """SELECT * FROM preprocessing_runs
                   WHERE observed_date = ? AND domain = 1
                   ORDER BY finished_at DESC LIMIT 1""",
                (target_date,),
            ).fetchone()
            history_count = conn.execute(
                "SELECT COUNT(*) FROM manual_review_history WHERE domain = 1"
            ).fetchone()[0]

        counts = {"total": len(items), "ready": 0, "needs_review": 0, "excluded": 0, "blocked": 0}
        auto_counts = {k: 0 for k in AUTO_CLASS_LABELS}
        manual_counts = {k: 0 for k in MANUAL_STATUS_LABELS}
        issue_counts: Dict[str, int] = {k: 0 for k in ISSUE_LABELS}
        ai_counts = {
            "cached_count": 0,
            "v2_cached_count": 0,
            "v1_legacy_count": 0,
            "suggest_exclude_count": 0,
            "suggest_exclude_unreviewed_count": 0,
            "graphic_print_count": 0,
        }
        stale_count = 0
        cross_cat_count = 0

        for item in items:
            counts[item["eligibility"]] += 1
            auto_counts[item["automatic_classification"]] += 1
            manual_counts[item["manual_review_status"]] += 1
            if item.get("ai_analysis"):
                ai_counts["cached_count"] += 1
                if item["ai_analysis"].get("prompt_version") == VISION_PROMPT_VERSION:
                    ai_counts["v2_cached_count"] += 1
                else:
                    ai_counts["v1_legacy_count"] += 1
                if item["ai_analysis"].get("is_graphic_print"):
                    ai_counts["graphic_print_count"] += 1
            if item.get("ai_suggest_exclude"):
                ai_counts["suggest_exclude_count"] += 1
                if item["manual_review_status"] == "unreviewed":
                    ai_counts["suggest_exclude_unreviewed_count"] += 1
            if item["review_stale"]:
                stale_count += 1
            if item["is_cross_category"]:
                cross_cat_count += 1
            for code in item["data_quality_issues"]:
                issue_counts[code] = issue_counts.get(code, 0) + 1

        total_obs = latest_run_row["total_observations"] if latest_run_row else len(items)

        category_breakdown = []
        for spec in CATEGORIES:
            matched = [
                i for i in items
                if spec.node_id and any(s.get("node_id") == spec.node_id for s in i["source_categories"])
            ]
            category_breakdown.append(
                {
                    "key": spec.key,
                    "node_id": spec.node_id,
                    "name": spec.name,
                    "short_name": spec.short_name,
                    "en_name": spec.en_name,
                    "garment_type": spec.garment_type,
                    "garment_label": spec.garment_label,
                    "gender": spec.gender,
                    "gender_label": spec.gender_label,
                    "is_configured": spec.is_configured,
                    "node_status": spec.node_status,
                    "color": spec.color,
                    "total": len(matched),
                    "ready": sum(1 for i in matched if i["eligibility"] == "ready"),
                    "needs_review": sum(1 for i in matched if i["eligibility"] == "needs_review"),
                    "excluded": sum(1 for i in matched if i["eligibility"] == "excluded"),
                }
            )

        return {
            "ready": True,
            "db_status": db_status.to_dict(),
            "selected_date": target_date,
            "available_dates": available_dates,
            "rule_version": RULE_VERSION,
            "categories": serialize_categories(),
            "category_breakdown": category_breakdown,
            "counts": counts,
            "total_observations": total_obs,
            "cross_category_asin_count": cross_cat_count,
            "automatic_counts": auto_counts,
            "manual_counts": manual_counts,
            "ai_counts": ai_counts,
            "stale_review_count": stale_count,
            "manual_history_count": history_count,
            "issue_counts": {k: v for k, v in issue_counts.items() if v > 0},
            "rules_summary": {
                "deduplication": {
                    "title": "商品去重",
                    "status": "completed",
                    "total_observations": total_obs,
                    "unique_asins": counts["total"],
                    "cross_category_merged": cross_cat_count,
                    "parent_same_flagged": issue_counts.get("parent_asin_same", 0),
                },
                "quality_check": {
                    "title": "数据质量检查",
                    "status": "completed",
                    "flagged_products": sum(1 for i in items if i["data_quality_issues"]),
                    "price_zero": issue_counts.get("price_zero", 0),
                    "price_anomaly": issue_counts.get("price_anomaly", 0),
                    "sales_suspect": issue_counts.get("sales_suspect", 0),
                    "rating_missing": issue_counts.get("rating_missing", 0),
                    "bsr_missing": issue_counts.get("bsr_missing", 0),
                    "image_missing": issue_counts.get("image_missing", 0),
                },
                "print_screening": {
                    "title": "印花相关性初筛",
                    "status": "advisory_only",
                    "possible_print": auto_counts["possible_print"],
                    "suspected_non_print": auto_counts["suspected_non_print"],
                    "conflicting": auto_counts["conflicting"],
                    "uncertain": auto_counts["uncertain"],
                },
            },
            "latest_run": dict(latest_run_row) if latest_run_row else None,
            "exclude_reasons": MANUAL_EXCLUDE_REASONS,
            "keep_reasons": MANUAL_KEEP_REASONS,
        }

    def get_unreviewed_products(self, observed_date: Optional[str] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Return all `unreviewed` hydrated products for the selected snapshot date."""
        if self.repo.status().state != "ready":
            return observed_date, []
        target_date = self._ensure_preprocessed(observed_date)
        items = self._hydrate_rows(target_date)
        return target_date, [i for i in items if i["manual_review_status"] == "unreviewed"]

    def query_products(
        self,
        *,
        observed_date: Optional[str] = None,
        category: str = "all",
        garment_type: str = "all",
        gender: str = "all",
        eligibility: str = "all",
        auto_class: str = "all",
        review_status: str = "all",
        issue: str = "all",
        ai_filter: str = "all",
        q: str = "",
        sort: str = "needs_review_first",
        order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        db_status = self.repo.status()
        if db_status.state != "ready":
            return {
                "ready": False,
                "db_status": db_status.to_dict(),
                "total": 0,
                "page": 1,
                "page_size": page_size,
                "total_pages": 0,
                "items": [],
            }

        target_date = self._ensure_preprocessed(observed_date)
        items = self._hydrate_rows(target_date)

        if category and category != "all":
            spec = resolve_category(category)
            if spec is not None:
                if spec.is_configured and spec.node_id:
                    items = [
                        i for i in items
                        if any(s.get("node_id") == spec.node_id for s in i["source_categories"])
                    ]
                else:
                    items = []
            else:
                items = [
                    i for i in items
                    if any(s.get("node_id") == category or s.get("category_key") == category for s in i["source_categories"])
                ]

        if garment_type and garment_type != "all":
            items = [i for i in items if garment_type in i.get("garment_types", [])]

        if gender and gender != "all":
            items = [i for i in items if gender in i.get("genders", [])]

        if eligibility and eligibility != "all":
            items = [i for i in items if i["eligibility"] == eligibility]
        if auto_class and auto_class != "all":
            items = [i for i in items if i["automatic_classification"] == auto_class]
        if review_status and review_status != "all":
            if review_status == "stale":
                items = [i for i in items if i["review_stale"]]
            else:
                items = [i for i in items if i["manual_review_status"] == review_status]
        if issue and issue != "all":
            if issue == "any":
                items = [i for i in items if len(i["data_quality_issues"]) > 0]
            elif issue == "clean":
                items = [i for i in items if len(i["data_quality_issues"]) == 0]
            else:
                items = [i for i in items if issue in i["data_quality_issues"]]

        ai_mode = (ai_filter or "all").strip().lower()
        if ai_mode == "suggest_exclude":
            items = [i for i in items if i.get("ai_suggest_exclude")]
        elif ai_mode == "graphic_print":
            items = [i for i in items if i.get("ai_analysis") and i["ai_analysis"].get("is_graphic_print")]
        elif ai_mode == "cached":
            items = [i for i in items if i.get("ai_analysis")]
        elif ai_mode == "uncached":
            items = [i for i in items if not i.get("ai_analysis")]

        query = q.strip().lower()
        if query:
            items = [
                i
                for i in items
                if query in i["asin"].lower()
                or (i["parent_asin"] and query in i["parent_asin"].lower())
                or query in i["title"].lower()
                or (i["brand"] and query in i["brand"].lower())
            ]

        desc = order.lower() != "asc"
        if sort == "needs_review_first":
            if ai_mode == "suggest_exclude":
                items.sort(
                    key=lambda i: (
                        0 if i["manual_review_status"] == "unreviewed" else 1,
                        -((i.get("ai_analysis") or {}).get("confidence") or 0.0),
                        -(i["listing_sales_30d"] if i["listing_sales_30d"] is not None else -1),
                        i["asin"],
                    )
                )
            else:
                priority = {"needs_review": 0, "blocked": 1, "ready": 2, "excluded": 3}
                auto_priority = {
                    "suspected_non_print": 0,
                    "conflicting": 1,
                    "uncertain": 2,
                    "possible_print": 3,
                }
                items.sort(
                    key=lambda i: (
                        priority.get(i["eligibility"], 9),
                        0 if i["review_stale"] else 1,
                        auto_priority.get(i["automatic_classification"], 9),
                        -(i["listing_sales_30d"] if i["listing_sales_30d"] is not None else -1),
                        i["asin"],
                    )
                )
        else:
            def sort_val(item: Dict[str, Any]) -> Any:
                if sort == "price":
                    return item["price"] if item["price_valid"] else None
                if sort == "rating_stars":
                    return item["rating_stars"]
                if sort == "ratings_count":
                    return item["ratings_count"]
                if sort == "main_category_bsr":
                    return item["main_category_bsr"]
                return item.get("listing_sales_30d")

            present = [i for i in items if sort_val(i) is not None]
            missing = [i for i in items if sort_val(i) is None]
            present.sort(key=lambda i: (sort_val(i), i["asin"]), reverse=desc)
            missing.sort(key=lambda i: i["asin"])
            items = present + missing

        safe_size = max(5, min(100, int(page_size)))
        total = len(items)
        total_pages = max(1, math.ceil(total / safe_size)) if total > 0 else 0
        safe_page = max(1, min(int(page), total_pages or 1))
        start = (safe_page - 1) * safe_size

        return {
            "ready": True,
            "selected_date": target_date,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "total_pages": total_pages,
            "items": items[start : start + safe_size],
        }

    def get_product_detail(self, asin: str, observed_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if self.repo.status().state != "ready":
            return None
        target_date = self._ensure_preprocessed(observed_date)
        target_asin = asin.strip().upper()
        items = self._hydrate_rows(target_date)
        found = next((i for i in items if i["asin"] == target_asin), None)
        if not found:
            return None

        with self._connect_app_db() as conn:
            history_rows = conn.execute(
                """SELECT id, action, prev_status, new_status, reason_code, reason_note,
                          operator_source, basis_fingerprint, request_id, created_at
                   FROM manual_review_history
                   WHERE domain = 1 AND asin = ?
                   ORDER BY id DESC""",
                (target_asin,),
            ).fetchall()

        history = []
        for h in history_rows:
            rc = h["reason_code"]
            history.append(
                {
                    **dict(h),
                    "reason_label": MANUAL_EXCLUDE_REASONS.get(rc) or MANUAL_KEEP_REASONS.get(rc) or rc,
                }
            )
        found["review_history"] = history
        return found

    def submit_manual_review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist human review action (`confirm_keep`, `confirm_exclude`, `restore`) transactionally.

        Validates parameters server-side and guards against duplicate requests via `request_id`.
        """
        if not isinstance(payload, dict):
            raise PreprocessingValidationError("请求体必须为 JSON 对象。")

        asin = str(payload.get("asin") or "").strip().upper()
        if not ASIN_RE.fullmatch(asin):
            raise PreprocessingValidationError("ASIN 必须为 10 位大写字母或数字。")

        action = str(payload.get("action") or "").strip().lower()
        if action not in ("confirm_keep", "confirm_exclude", "restore"):
            raise PreprocessingValidationError("不支持的操作类型，仅允许 confirm_keep、confirm_exclude 或 restore。")

        reason_code = str(payload.get("reason_code") or "").strip() or None
        reason_note = str(payload.get("reason_note") or "").strip()[:500]
        operator_source = str(payload.get("operator_source") or "workbench_ui").strip()[:60]
        request_id = str(payload.get("request_id") or "").strip()[:120] or None
        observed_date = payload.get("observed_date")

        if action == "confirm_exclude":
            if reason_code and reason_code not in MANUAL_EXCLUDE_REASONS:
                raise PreprocessingValidationError(f"无效的排除原因代码：{reason_code}。")
            if not reason_code:
                reason_code = "other"
            new_status = "confirmed_exclude"
        elif action == "confirm_keep":
            if reason_code and reason_code not in MANUAL_KEEP_REASONS:
                raise PreprocessingValidationError(f"无效的保留原因代码：{reason_code}。")
            if not reason_code:
                reason_code = "keep_for_scoring"
            new_status = "confirmed_keep"
        else:
            reason_code = None
            new_status = "unreviewed"

        target_date = self._ensure_preprocessed(observed_date)
        if not target_date:
            raise PreprocessingNotFoundError("当前无可用的已预处理数据快照。")

        with self._lock:
            with self._connect_app_db() as conn:
                prod = conn.execute(
                    """SELECT asin, basis_fingerprint FROM preprocessed_products
                       WHERE observed_date = ? AND domain = 1 AND asin = ?""",
                    (target_date, asin),
                ).fetchone()
                if prod is None:
                    raise PreprocessingNotFoundError(f"未在当前快照中找到商品 ASIN：{asin}。")

                # Duplicate request protection via request_id
                if request_id:
                    dup = conn.execute(
                        "SELECT id FROM manual_review_history WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if dup is not None:
                        detail = self.get_product_detail(asin, observed_date=target_date)
                        return {
                            "deduplicated": True,
                            "item": detail,
                            "overview": self.get_overview(observed_date=target_date),
                        }

                existing = conn.execute(
                    "SELECT * FROM manual_reviews WHERE domain = 1 AND asin = ?",
                    (asin,),
                ).fetchone()
                prev_status = existing["review_status"] if existing else "unreviewed"
                fp = prod["basis_fingerprint"]
                now_iso = self._now_iso()

                # Also guard against identical back-to-back no-op state transition when no note changed
                if (
                    existing is not None
                    and prev_status == new_status
                    and existing["basis_fingerprint"] == fp
                    and (existing["reason_code"] or None) == reason_code
                    and (existing["reason_note"] or "") == reason_note
                ):
                    detail = self.get_product_detail(asin, observed_date=target_date)
                    return {
                        "deduplicated": True,
                        "item": detail,
                        "overview": self.get_overview(observed_date=target_date),
                    }

                conn.execute("BEGIN IMMEDIATE")
                if new_status == "unreviewed":
                    conn.execute(
                        "DELETE FROM manual_reviews WHERE domain = 1 AND asin = ?",
                        (asin,),
                    )
                else:
                    conn.execute(
                        """INSERT INTO manual_reviews
                           (domain, asin, review_status, reason_code, reason_note, operator_source,
                            reviewed_at, basis_fingerprint, last_request_id)
                           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(domain, asin) DO UPDATE SET
                             review_status = excluded.review_status,
                             reason_code = excluded.reason_code,
                             reason_note = excluded.reason_note,
                             operator_source = excluded.operator_source,
                             reviewed_at = excluded.reviewed_at,
                             basis_fingerprint = excluded.basis_fingerprint,
                             last_request_id = excluded.last_request_id""",
                        (asin, new_status, reason_code, reason_note, operator_source, now_iso, fp, request_id),
                    )

                conn.execute(
                    """INSERT INTO manual_review_history
                       (domain, asin, action, prev_status, new_status, reason_code, reason_note,
                        operator_source, basis_fingerprint, request_id, created_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        asin, action, prev_status, new_status, reason_code, reason_note,
                        operator_source, fp, request_id or f"auto_{secrets.token_hex(8)}", now_iso,
                    ),
                )
                conn.commit()

        detail = self.get_product_detail(asin, observed_date=target_date)
        return {
            "deduplicated": False,
            "item": detail,
            "overview": self.get_overview(observed_date=target_date),
        }

    def submit_batch_review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Batch human-review update by `asins[]`, `brand`, or `parent_asin`.

        Supports `dry_run=True` to preview affected items before confirming.
        Every updated ASIN is written transactionally to `manual_reviews` and logged
        individually in `manual_review_history` so all batch operations remain 100%
        undoable and traceable.
        """
        if not isinstance(payload, dict):
            raise PreprocessingValidationError("请求体必须为 JSON 对象。")

        raw_action = str(payload.get("action") or payload.get("review_status") or "").strip().lower()
        action_alias = {
            "confirm_keep": "confirm_keep",
            "confirmed_keep": "confirm_keep",
            "keep": "confirm_keep",
            "confirm_exclude": "confirm_exclude",
            "confirmed_exclude": "confirm_exclude",
            "exclude": "confirm_exclude",
            "restore": "restore",
            "unreviewed": "restore",
            "reset": "restore",
        }
        action = action_alias.get(raw_action)
        if not action:
            raise PreprocessingValidationError(
                "不支持的批量操作类型，仅允许 confirm_keep / confirmed_keep、confirm_exclude / confirmed_exclude 或 restore / unreviewed。"
            )

        reason_code = str(payload.get("reason_code") or "").strip() or None
        reason_note = str(payload.get("reason_note") or "").strip()[:500]
        operator_source = str(payload.get("operator_source") or "workbench_batch_ui").strip()[:60]
        request_id = str(payload.get("request_id") or "").strip()[:100] or f"batch_{secrets.token_hex(6)}"
        observed_date = payload.get("observed_date")
        dry_run = bool(payload.get("dry_run", False))

        if action == "confirm_exclude":
            if reason_code and reason_code not in MANUAL_EXCLUDE_REASONS:
                raise PreprocessingValidationError(f"无效的排除原因代码：{reason_code}。")
            if not reason_code:
                reason_code = "solid_non_print"
            new_status = "confirmed_exclude"
        elif action == "confirm_keep":
            if reason_code and reason_code not in MANUAL_KEEP_REASONS:
                raise PreprocessingValidationError(f"无效的保留原因代码：{reason_code}。")
            if not reason_code:
                reason_code = "keep_for_scoring"
            new_status = "confirmed_keep"
        else:
            reason_code = None
            new_status = "unreviewed"

        raw_asins = payload.get("asins")
        brand_val = str(payload.get("brand") or "").strip()
        parent_val = str(payload.get("parent_asin") or "").strip().upper()

        selector_count = int(bool(raw_asins)) + int(bool(brand_val)) + int(bool(parent_val))
        if selector_count == 0:
            raise PreprocessingValidationError("请至少提供 asins、brand 或 parent_asin 之一作为批量目标。")

        target_asins_input: List[str] = []
        if raw_asins is not None:
            if not isinstance(raw_asins, list) or not raw_asins:
                raise PreprocessingValidationError("asins 必须为非空数组。")
            seen = set()
            for a in raw_asins:
                norm = str(a or "").strip().upper()
                if not ASIN_RE.fullmatch(norm):
                    raise PreprocessingValidationError(f"批量列表中存在无效 ASIN：{a}。")
                if norm not in seen:
                    seen.add(norm)
                    target_asins_input.append(norm)

        if parent_val and not ASIN_RE.fullmatch(parent_val):
            raise PreprocessingValidationError(f"无效的 parent_asin：{parent_val}。")

        target_date = self._ensure_preprocessed(observed_date)
        if not target_date:
            raise PreprocessingNotFoundError("当前无可用的已预处理数据快照。")

        with self._lock:
            with self._connect_app_db() as conn:
                if target_asins_input:
                    placeholders = ",".join("?" for _ in target_asins_input)
                    rows = conn.execute(
                        f"""SELECT asin, title, brand, parent_asin, basis_fingerprint
                            FROM preprocessed_products
                            WHERE observed_date = ? AND domain = 1 AND asin IN ({placeholders})
                            ORDER BY asin""",
                        (target_date, *target_asins_input),
                    ).fetchall()
                    scope_label = f"batch_asins({len(rows)})"
                elif brand_val:
                    rows = conn.execute(
                        """SELECT asin, title, brand, parent_asin, basis_fingerprint
                           FROM preprocessed_products
                           WHERE observed_date = ? AND domain = 1
                             AND LOWER(TRIM(COALESCE(brand, ''))) = LOWER(TRIM(?))
                           ORDER BY asin""",
                        (target_date, brand_val),
                    ).fetchall()
                    scope_label = f"batch_brand:{brand_val[:40]}"
                else:
                    rows = conn.execute(
                        """SELECT asin, title, brand, parent_asin, basis_fingerprint
                           FROM preprocessed_products
                           WHERE observed_date = ? AND domain = 1
                             AND UPPER(TRIM(COALESCE(parent_asin, ''))) = ?
                           ORDER BY asin""",
                        (target_date, parent_val),
                    ).fetchall()
                    scope_label = f"batch_parent:{parent_val}"

                if not rows:
                    raise PreprocessingNotFoundError("未在当前快照中匹配到任何可更新商品。")

                matched_asins = [r["asin"] for r in rows]
                if dry_run:
                    return {
                        "dry_run": True,
                        "selected_date": target_date,
                        "action": action,
                        "new_status": new_status,
                        "scope": scope_label,
                        "matched_count": len(matched_asins),
                        "affected_asins": matched_asins,
                        "sample_titles": [r["title"] for r in rows[:5]],
                    }

                now_iso = self._now_iso()
                effective_source = f"{operator_source}|{scope_label}"[:60]
                updated_asins: List[str] = []
                skipped_asins: List[str] = []

                conn.execute("BEGIN IMMEDIATE")
                for row in rows:
                    asin = row["asin"]
                    fp = row["basis_fingerprint"]
                    item_req_id = f"{request_id}:{asin}"

                    dup = conn.execute(
                        "SELECT id FROM manual_review_history WHERE request_id = ?",
                        (item_req_id,),
                    ).fetchone()
                    if dup is not None:
                        skipped_asins.append(asin)
                        continue

                    existing = conn.execute(
                        "SELECT * FROM manual_reviews WHERE domain = 1 AND asin = ?",
                        (asin,),
                    ).fetchone()
                    prev_status = existing["review_status"] if existing else "unreviewed"

                    if (
                        existing is not None
                        and prev_status == new_status
                        and existing["basis_fingerprint"] == fp
                        and (existing["reason_code"] or None) == reason_code
                        and (existing["reason_note"] or "") == reason_note
                    ):
                        skipped_asins.append(asin)
                        continue
                    if existing is None and new_status == "unreviewed":
                        skipped_asins.append(asin)
                        continue

                    if new_status == "unreviewed":
                        conn.execute(
                            "DELETE FROM manual_reviews WHERE domain = 1 AND asin = ?",
                            (asin,),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO manual_reviews
                               (domain, asin, review_status, reason_code, reason_note, operator_source,
                                reviewed_at, basis_fingerprint, last_request_id)
                               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(domain, asin) DO UPDATE SET
                                 review_status = excluded.review_status,
                                 reason_code = excluded.reason_code,
                                 reason_note = excluded.reason_note,
                                 operator_source = excluded.operator_source,
                                 reviewed_at = excluded.reviewed_at,
                                 basis_fingerprint = excluded.basis_fingerprint,
                                 last_request_id = excluded.last_request_id""",
                            (
                                asin, new_status, reason_code, reason_note, effective_source,
                                now_iso, fp, item_req_id,
                            ),
                        )

                    conn.execute(
                        """INSERT INTO manual_review_history
                           (domain, asin, action, prev_status, new_status, reason_code, reason_note,
                            operator_source, basis_fingerprint, request_id, created_at)
                           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            asin, action, prev_status, new_status, reason_code, reason_note,
                            effective_source, fp, item_req_id, now_iso,
                        ),
                    )
                    updated_asins.append(asin)
                conn.commit()

        return {
            "dry_run": False,
            "batch_id": request_id,
            "selected_date": target_date,
            "action": action,
            "new_status": new_status,
            "scope": scope_label,
            "matched_count": len(matched_asins),
            "updated_count": len(updated_asins),
            "skipped_count": len(skipped_asins),
            "affected_asins": updated_asins,
            "matched_asins": matched_asins,
            "overview": self.get_overview(observed_date=target_date),
        }

    def get_dashboard_filter_sets(self, observed_date: Optional[str] = None) -> Dict[str, Any]:
        """Return ASIN sets linking Stage 02 Preprocessing decisions into Stage 03 Market Analytics."""
        if self.repo.status().state != "ready":
            return {
                "ready": False,
                "excluded_asins": set(),
                "printable_asins": set(),
                "summary": {"total": 0, "printable_count": 0, "excluded_count": 0, "needs_review_count": 0},
            }
        target_date = self._ensure_preprocessed(observed_date)
        items = self._hydrate_rows(target_date)
        excluded_asins = {i["asin"] for i in items if i["manual_review_status"] == "confirmed_exclude"}
        printable_asins = {
            i["asin"]
            for i in items
            if i["manual_review_status"] == "confirmed_keep"
            or (
                i["manual_review_status"] != "confirmed_exclude"
                and i["automatic_classification"] == "possible_print"
            )
        }
        needs_review_count = sum(1 for i in items if i["eligibility"] == "needs_review")
        return {
            "ready": True,
            "selected_date": target_date,
            "excluded_asins": excluded_asins,
            "printable_asins": printable_asins,
            "summary": {
                "total": len(items),
                "printable_count": len(printable_asins),
                "excluded_count": len(excluded_asins),
                "needs_review_count": needs_review_count,
            },
        }

    def get_scoring_candidates(
        self,
        *,
        observed_date: Optional[str] = None,
        category: str = "all",
        garment_type: str = "all",
        gender: str = "all",
        eligibility: str = "ready",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Standardized Stage 04 Candidate Scoring input API (multi-category aware).

        Defaults strictly to `eligibility=ready`. Never leaks `excluded`, `needs_review`, or `blocked`
        into ready candidates, never duplicates ASINs, never fills missing values, never fabricates scores,
        and never imposes undecided cross-category Top 10 quota rules.
        """
        allowed_elig = {"ready", "needs_review", "excluded", "blocked"}
        eff_elig = eligibility if eligibility in allowed_elig else "ready"
        result = self.query_products(
            observed_date=observed_date,
            category=category,
            garment_type=garment_type,
            gender=gender,
            eligibility=eff_elig,
            sort="listing_sales_30d",
            order="desc",
            page=page,
            page_size=page_size,
        )
        if not result.get("ready"):
            return {
                "contract_version": "hotprint-scoring-input-v0.2",
                "ready": False,
                "eligibility_filter": eff_elig,
                "category_filter": category,
                "garment_type_filter": garment_type,
                "gender_filter": gender,
                "categories": serialize_categories(),
                "cross_category_quota_rule_defined": False,
                "total": 0,
                "items": [],
            }

        # Project exact standardized downstream contract fields (including multi-category provenance)
        projected = [
            {
                "site": item["site"],
                "domain": item["domain"],
                "asin": item["asin"],
                "parent_asin": item["parent_asin"],
                "title": item["title"],
                "brand": item["brand"],
                "image_url": item["image_url"],
                "source_categories": item["source_categories"],
                "source_observations": item["source_observations"],
                "garment_types": item["garment_types"],
                "genders": item["genders"],
                "is_cross_category": item["is_cross_category"],
                "cross_category_count": item["cross_category_count"],
                "listing_sales_30d": item["listing_sales_30d"],
                "price": item["price"],
                "rating_stars": item["rating_stars"],
                "ratings_count": item["ratings_count"],
                "online_date": item["online_date"],
                "category_bsr_by_source": item["category_bsr_by_source"],
                "data_quality_issues": item["data_quality_issues"],
                "automatic_classification": item["automatic_classification"],
                "automatic_reasons": item["automatic_reasons"],
                "manual_review_status": item["manual_review_status"],
                "manual_review_reason": item["manual_review_reason"],
                "manual_review_note": item["manual_review_note"],
                "review_stale": item["review_stale"],
                "eligibility": item["eligibility"],
                "visual_verified": False,
                "observed_at": item["observed_at"],
                "rule_version": item["rule_version"],
            }
            for item in result["items"]
        ]

        return {
            "contract_version": "hotprint-scoring-input-v0.2",
            "ready": True,
            "selected_date": result["selected_date"],
            "eligibility_filter": eff_elig,
            "category_filter": category,
            "garment_type_filter": garment_type,
            "gender_filter": gender,
            "categories": serialize_categories(),
            "rule_version": RULE_VERSION,
            "scoring_formula_implemented": False,
            "cross_category_quota_rule_defined": False,
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
            "items": projected,
        }
