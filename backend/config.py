"""Configuration, category registry, and safety guards for HotPrint Workbench."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
SORFTIME_DIR = WORKBENCH_ROOT.parent
DEFAULT_DB_PATH = SORFTIME_DIR / "category_sales.sqlite3"
DEFAULT_LIVE_DB_PATH = WORKBENCH_ROOT / "data" / "category_sales_live.sqlite3"
API_KEY_PATH = SORFTIME_DIR / "api_key.local"
WORKBENCH_API_KEY_PATH = WORKBENCH_ROOT / "data" / "api_key.local"
WORKBENCH_AI_CONFIG_PATH = WORKBENCH_ROOT / "data" / "ai_config.local"
WORKBENCH_SUPABASE_CONFIG_PATH = WORKBENCH_ROOT / "data" / "supabase_config.local"
DEFAULT_SUPABASE_URL = "https://oyckbvstitrmpvnvityb.supabase.co"
DEFAULT_AI_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_AI_MODEL = "deepseek-flash"
FRONTEND_DIR = WORKBENCH_ROOT / "frontend"

# Vision Prompt v2 controlled vocabularies & versioning (P2: incremental migration)
VISION_PROMPT_VERSION: str = "v2"

SEASON_TAGS: Tuple[str, ...] = (
    "evergreen",
    "christmas",
    "thanksgiving",
    "halloween",
    "valentines",
    "easter",
    "summer",
    "back_to_school",
    "other_seasonal",
)

SEASON_TAG_LABELS: Dict[str, str] = {
    "evergreen": "四季常青",
    "christmas": "圣诞节",
    "thanksgiving": "感恩节",
    "halloween": "万圣节",
    "valentines": "情人节",
    "easter": "复活节",
    "summer": "夏季",
    "back_to_school": "开学季",
    "other_seasonal": "其他季节",
}

# Holiday calendar for Stage 06 POD seasonal countdown (displays days remaining only; no hard gate)
HOLIDAY_DATES: Dict[str, str] = {
    "thanksgiving": "2026-11-26",
    "christmas": "2026-12-25",
    "valentines": "2027-02-14",
    "easter": "2027-03-28",
    "summer": "2027-06-21",
    "back_to_school": "2027-08-15",
    "halloween": "2027-10-31",
}

TRANSFERABILITY_TYPES: Tuple[str, ...] = (
    "pure_graphic",
    "text_pun",
    "form_dependent",
)

TRANSFERABILITY_LABELS: Dict[str, str] = {
    "pure_graphic": "纯图可迁移",
    "text_pun": "文字梗可迁移",
    "form_dependent": "版型/工艺依赖",
}

MOTIF_VOCABULARY: Tuple[str, ...] = (
    "dog",
    "cat",
    "turkey",
    "pumpkin",
    "ghost",
    "skeleton",
    "witch",
    "santa",
    "christmas_tree",
    "snowman",
    "reindeer",
    "grinch_like",
    "heart",
    "bunny",
    "bear",
    "frog",
    "duck",
    "cow",
    "dinosaur",
    "butterfly",
    "floral",
    "botanical",
    "mushroom",
    "sun_moon",
    "retro_sunset",
    "mountain",
    "camping",
    "beach_ocean",
    "coffee",
    "wine_beer",
    "food_snack",
    "music_guitar",
    "sports",
    "gaming",
    "reading_books",
    "teacher_school",
    "nurse_medical",
    "mom_dad_family",
    "patriotic_flag",
    "western_cowboy",
    "skull_gothic",
    "smiley_face",
    "checkerboard_pattern",
    "leopard_animal_print",
    "text_only",
    "other",
)

MOTIF_LABELS: Dict[str, str] = {
    "dog": "小狗",
    "cat": "猫咪",
    "turkey": "火鸡",
    "pumpkin": "南瓜",
    "ghost": "幽灵",
    "skeleton": "骷髅骨架",
    "witch": "女巫",
    "santa": "圣诞老人",
    "christmas_tree": "圣诞树",
    "snowman": "雪人",
    "reindeer": "驯鹿",
    "grinch_like": "绿毛怪/格林奇风",
    "heart": "爱心",
    "bunny": "兔子",
    "bear": "小熊",
    "frog": "青蛙",
    "duck": "鸭子",
    "cow": "奶牛",
    "dinosaur": "恐龙",
    "butterfly": "蝴蝶",
    "floral": "花卉",
    "botanical": "植物绿叶",
    "mushroom": "蘑菇",
    "sun_moon": "日月星辰",
    "retro_sunset": "复古落日",
    "mountain": "山峰",
    "camping": "露营户外",
    "beach_ocean": "海滩海洋",
    "coffee": "咖啡",
    "wine_beer": "酒水/啤酒",
    "food_snack": "美食零食",
    "music_guitar": "音乐/吉他",
    "sports": "运动体育",
    "gaming": "游戏电竞",
    "reading_books": "阅读书籍",
    "teacher_school": "教师校园",
    "nurse_medical": "医护医疗",
    "mom_dad_family": "亲子家庭",
    "patriotic_flag": "星条旗/爱国",
    "western_cowboy": "西部牛仔",
    "skull_gothic": "暗黑头骨",
    "smiley_face": "笑脸",
    "checkerboard_pattern": "棋盘格",
    "leopard_animal_print": "豹纹动物纹",
    "text_only": "纯文字标语",
    "other": "其他主体",
}

# Data credibility audit thresholds (flag + confidence penalty + outlier trim only; never auto-exclude)
PRICE_ANOMALY_THRESHOLD_USD: float = 5.0
SALES_BSR_RESIDUAL_SIGMA: float = 2.0
SALES_BSR_MIN_SAMPLE_SIZE: int = 30

# Stage 04 Low-Rating Penalty thresholds & multipliers (active when rating_stars is not None and ratings_count >= 1)
LOW_RATING_SEVERE_THRESHOLD: float = 3.0
LOW_RATING_MILD_THRESHOLD: float = 3.8
LOW_RATING_SEVERE_MULTIPLIER: float = 0.55
LOW_RATING_MILD_MULTIPLIER: float = 0.85

BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8772
DEFAULT_DOMAIN = 1
DOMAIN_LABELS: Dict[int, str] = {
    1: "Amazon US (美国站)",
}

GARMENT_TYPE_LABELS: Dict[str, str] = {
    "t_shirt": "T 恤",
    "sweatshirt": "卫衣",
    "signal": "跨品类信号",
}

GENDER_LABELS: Dict[str, str] = {
    "women": "女装",
    "men": "男装",
    "unisex": "通用",
}

CATEGORY_ROLE_LABELS: Dict[str, str] = {
    "core": "核心选品类目",
    "signal": "跨品类信号类目",
}


@dataclass(frozen=True)
class CategorySpec:
    key: str
    node_id: Optional[str]
    name: str
    short_name: str
    en_name: str
    garment_type: str
    gender: str
    segment: str
    color: str
    soft_color: str
    category_role: str = "core"

    @property
    def is_configured(self) -> bool:
        return bool(self.node_id and self.node_id.strip())

    @property
    def node_status(self) -> str:
        return "configured" if self.is_configured else "pending_config"

    @property
    def status_label(self) -> str:
        return "已配置 Node ID" if self.is_configured else "待配置"

    @property
    def garment_label(self) -> str:
        return GARMENT_TYPE_LABELS.get(self.garment_type, self.garment_type)

    @property
    def gender_label(self) -> str:
        return GENDER_LABELS.get(self.gender, self.gender)

    @property
    def role_label(self) -> str:
        return CATEGORY_ROLE_LABELS.get(self.category_role, self.category_role)


# Unified single source of truth for the 6 target Amazon US categories.
# All 6 Browse Node IDs have been verified from Amazon US Best Sellers URLs.
# Any future category with node_id=None is treated as "待配置" and blocked from collection.
CATEGORIES: Tuple[CategorySpec, ...] = (
    CategorySpec(
        key="womens_tshirts",
        node_id="1044544",
        name="女式 T 恤",
        short_name="女T恤",
        en_name="Women's T-Shirts",
        garment_type="t_shirt",
        gender="women",
        segment="standard",
        color="#E11D48",
        soft_color="#FFF1F2",
    ),
    CategorySpec(
        key="mens_tshirts",
        node_id="1045624",
        name="男式 T 恤",
        short_name="男T恤",
        en_name="Men's T-Shirts",
        garment_type="t_shirt",
        gender="men",
        segment="standard",
        color="#0284C7",
        soft_color="#F0F9FF",
    ),
    CategorySpec(
        key="womens_novelty_tshirts",
        node_id="9056923011",
        name="女式趣味 T 恤",
        short_name="女趣T",
        en_name="Women's Novelty T-Shirts",
        garment_type="t_shirt",
        gender="women",
        segment="novelty",
        color="#EC4899",
        soft_color="#FDF2F8",
    ),
    CategorySpec(
        key="mens_novelty_tshirts",
        node_id="9056987011",
        name="男式趣味 T 恤",
        short_name="男趣T",
        en_name="Men's Novelty T-Shirts",
        garment_type="t_shirt",
        gender="men",
        segment="novelty",
        color="#2563EB",
        soft_color="#EFF6FF",
    ),
    CategorySpec(
        key="womens_novelty_sweatshirts",
        node_id="9056929011",
        name="女式趣味卫衣",
        short_name="女趣卫衣",
        en_name="Women's Novelty Sweatshirts",
        garment_type="sweatshirt",
        gender="women",
        segment="novelty",
        color="#D14D72",
        soft_color="#FDF2F5",
    ),
    CategorySpec(
        key="mens_novelty_sweatshirts",
        node_id="9056993011",
        name="男式趣味卫衣",
        short_name="男趣卫衣",
        en_name="Men's Novelty Sweatshirts",
        garment_type="sweatshirt",
        gender="men",
        segment="novelty",
        color="#0D7C8C",
        soft_color="#EEF8FA",
    ),
)

CONFIGURED_CATEGORIES: Tuple[CategorySpec, ...] = tuple(c for c in CATEGORIES if c.is_configured)
PENDING_CATEGORIES: Tuple[CategorySpec, ...] = tuple(c for c in CATEGORIES if not c.is_configured)
CATEGORY_BY_NODE: Dict[str, CategorySpec] = {c.node_id: c for c in CONFIGURED_CATEGORIES if c.node_id}
CATEGORY_BY_KEY: Dict[str, CategorySpec] = {c.key: c for c in CATEGORIES}

# Independent Cross-Category Signal Layer slots (P4: strictly isolated from 02->04 apparel pipeline).
# Default node_id=None (pending_config); server strictly blocks collection until a verified Node ID is filled.
SIGNAL_CATEGORIES: Tuple[CategorySpec, ...] = (
    CategorySpec(
        key="signal_mugs",
        node_id=None,
        name="马克杯",
        short_name="马克杯",
        en_name="Novelty & Graphic Coffee Mugs",
        garment_type="signal",
        gender="unisex",
        segment="signal",
        color="#D97706",
        soft_color="#FFFBEB",
        category_role="signal",
    ),
    CategorySpec(
        key="signal_tote_bags",
        node_id=None,
        name="帆布袋",
        short_name="帆布袋",
        en_name="Graphic Canvas Tote Bags",
        garment_type="signal",
        gender="unisex",
        segment="signal",
        color="#059669",
        soft_color="#ECFDF5",
        category_role="signal",
    ),
    CategorySpec(
        key="signal_phone_cases",
        node_id=None,
        name="手机壳",
        short_name="手机壳",
        en_name="Graphic Phone Cases",
        garment_type="signal",
        gender="unisex",
        segment="signal",
        color="#7C3AED",
        soft_color="#F5F3FF",
        category_role="signal",
    ),
)

ALL_CATEGORIES: Tuple[CategorySpec, ...] = CATEGORIES + SIGNAL_CATEGORIES
SIGNAL_CATEGORY_BY_KEY: Dict[str, CategorySpec] = {c.key: c for c in SIGNAL_CATEGORIES}
SIGNAL_CATEGORY_BY_NODE: Dict[str, CategorySpec] = {
    c.node_id: c for c in SIGNAL_CATEGORIES if c.is_configured and c.node_id
}

# Backward-compatible aliases for legacy callers ("women" / "men")
CATEGORY_KEY_ALIASES: Dict[str, str] = {
    "women": "womens_novelty_sweatshirts",
    "men": "mens_novelty_sweatshirts",
}


def resolve_category(
    identifier: Optional[str],
    *,
    include_signals: bool = False,
) -> Optional[CategorySpec]:
    """Resolve a category by verified node_id, canonical key, or legacy alias.

    Signal categories are excluded by default so core 02->06 apparel endpoints never mix them in.
    """
    if not identifier:
        return None
    text = str(identifier).strip()
    if not text:
        return None
    if text in CATEGORY_BY_NODE:
        return CATEGORY_BY_NODE[text]
    if text in CATEGORY_BY_KEY:
        return CATEGORY_BY_KEY[text]
    alias_key = CATEGORY_KEY_ALIASES.get(text)
    if alias_key and alias_key in CATEGORY_BY_KEY:
        return CATEGORY_BY_KEY[alias_key]
    if include_signals:
        if text in SIGNAL_CATEGORY_BY_NODE:
            return SIGNAL_CATEGORY_BY_NODE[text]
        if text in SIGNAL_CATEGORY_BY_KEY:
            return SIGNAL_CATEGORY_BY_KEY[text]
    return None


DEFAULT_COLLECTION_PARAMS = {
    "domain": DEFAULT_DOMAIN,
    "page": 1,
    "pages": 1,
    "range": 100,
    "estimated_credits_per_page": 5,
    "max_pages_allowed": 5,
    "max_range_allowed": 100,
    "min_range_allowed": 10,
    "default_budget_cap": 30,
    "max_budget_cap": 150,
}

# Business methodology disclosures shown across API & UI.
METHODOLOGY_NOTES: List[Dict[str, str]] = [
    {
        "id": "listing_sales_30d",
        "title": "近 30 天 Listing 估算销量",
        "summary": "来自 Sorftime 第三方估算模型，代表 Listing 维度的滚动 30 天估算值，不是 Amazon 真实订单量，也不能等同于单个印花图案的独立销量。",
    },
    {
        "id": "cross_category_dedup",
        "title": "两层去重口径（跨类目 ASIN 去重 + 同款/同介绍孤立子体折叠）",
        "summary": "第一层按站点 + ASIN 合并跨类目重复观察（覆盖全部已采目标类目，绝不重复累加销量）；第二层在市场看板针对同品牌商品折叠介绍/标题完全一致或同图高相似孤立变体，保留主代表款并完整保留全部来源类目与原始证据。",
    },
    {
        "id": "parent_child_sales",
        "title": "父子体关系与销量边界",
        "summary": "父体与子体销量不可随意叠加。当返回记录中 ParentAsin = Asin 时，标记为「父子编号相同，需核对」，隐藏直达商品链接，待人工进一步核验。",
    },
    {
        "id": "bsr_scope",
        "title": "BSR 类目上下文",
        "summary": "source_category_bsr 仅在对应源类目节点内有效，不同类目（如 T 恤与卫衣、男装与女装、普通与趣味类目）之间不可直接比较或混合排名。跨类目查看时请分别解读各源类目独立 BSR。",
    },
    {
        "id": "anomalies",
        "title": "0 价格与缺失值处理",
        "summary": "售价为 $0、售价缺失、评分为 0/空值均单独标记并保留原貌，绝不补值或虚构。价格均值/中位数/散点图自动剔除异常价格，并明确标注有效样本分母。",
    },
    {
        "id": "sample_scope",
        "title": "六类目覆盖与样本边界",
        "summary": "系统已扩展为六大目标服饰类目（女/男 T 恤、女/男趣味 T 恤、女/男趣味卫衣）；当前仅已核实 Node ID 的类目具备可采快照，未核实 Node ID 的类目标记为「待配置」且禁止发起真实采集。首页单日快照不代表全美市场规模或增长趋势。",
    },
]


def beijing_today() -> str:
    return datetime.now(BEIJING_TZ).date().isoformat()


def serialize_category(spec: CategorySpec) -> Dict[str, Any]:
    return {
        "key": spec.key,
        "node_id": spec.node_id,
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
        "is_configured": spec.is_configured,
        "node_status": spec.node_status,
        "status_label": spec.status_label,
        "color": spec.color,
        "soft_color": spec.soft_color,
    }


def serialize_categories() -> List[Dict[str, Any]]:
    return [serialize_category(c) for c in CATEGORIES]


def serialize_signal_categories() -> List[Dict[str, Any]]:
    return [serialize_category(c) for c in SIGNAL_CATEGORIES]
