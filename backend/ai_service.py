"""Unified DeepSeek / OpenAI-compatible Vision & Clustering Service + SQLite Cache.

Uses Python 3 standard library only (`urllib.request`, `sqlite3`, `concurrent.futures`).
- Default endpoint: `https://api.deepseek.com/v1`
- Default model: `deepseek-flash` (native multimodal text + `image_url`)
- API key is persisted ONLY in gitignored local file `data/ai_config.local` (chmod 0600)
  and is NEVER returned in plaintext or written to logs.
- Every product vision analysis is cached in `product_ai_cache` keyed by `asin` +
  `evidence_hash` (hash of title + main_image_url) so identical items are analyzed once.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import (
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MODEL,
    MOTIF_LABELS,
    MOTIF_VOCABULARY,
    SEASON_TAGS,
    SEASON_TAG_LABELS,
    TRANSFERABILITY_LABELS,
    TRANSFERABILITY_TYPES,
    VISION_PROMPT_VERSION,
    WORKBENCH_AI_CONFIG_PATH,
    WORKBENCH_ROOT,
)

DEFAULT_APP_DB_PATH = WORKBENCH_ROOT / "data" / "preprocessing.sqlite3"

AI_SUGGEST_EXCLUDE_CONFIDENCE = 0.85

PATTERN_TYPES: Tuple[str, ...] = (
    "graphic_print",
    "solid",
    "stripes",
    "polka_dot",
    "plaid",
    "texture_only",
    "uncertain",
)

PATTERN_TYPE_LABELS: Dict[str, str] = {
    "graphic_print": "印花图案",
    "solid": "纯色素衫",
    "stripes": "纯条纹",
    "polka_dot": "纯波点",
    "plaid": "纯格纹",
    "texture_only": "仅肌理/面料",
    "uncertain": "视觉存疑",
}

FORBIDDEN_CLUSTER_EXACT = {
    "其他",
    "其它",
    "杂项",
    "其他类",
    "其他主题",
    "其他印花",
    "未分类",
    "杂类",
    "兜底",
    "兜底组",
    "其他/杂项",
    "其他风格",
    "混合",
    "综合",
    "综合其他",
    "other",
    "others",
    "misc",
    "miscellaneous",
    "uncategorized",
    "unclassified",
    "mixed",
    "various",
}

FORBIDDEN_CLUSTER_PATTERNS = (
    re.compile(r"^(?:其他|其它|杂项|未分类|兜底|杂类)(?:[/、\s\-—]*(?:其他|其它|杂项|未分类|主题|印花|风格|款式|分组|类))?$"),
    re.compile(r"(?:杂项|未分类|兜底分组|垃圾桶)"),
    re.compile(r"^(?:other|others|misc|miscellaneous|uncategorized)(?:\s+(?:prints?|themes?|items?|styles?))?$", re.IGNORECASE),
)

AI_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product_ai_cache (
    asin TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
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

CREATE INDEX IF NOT EXISTS idx_product_ai_cache_filter
    ON product_ai_cache(is_graphic_print, confidence, pattern_type);
"""

VISION_SYSTEM_PROMPT = f"""你是一位资深的跨境电商服饰选品与印花设计视觉分析专家。
你的任务是根据商品主图、标题与品牌信息，完成两件事：
1. 判断衣服表面是否包含有设计开发价值的独立印花图案、插画、文字标语或刺绣/贴布设计（用于剔除纯色打底衫、纯条纹、纯波点、纯格纹、纯针织肌理等无印花基础款）。
2. 如果是有效印花/图案服饰，精准提取其核心印花主体、衣服上的英文/文字文案 OCR、视觉风格、2~4 个规范化主题词组，以及严格受控的季节标签（season_tag）、核心图案主体词表（motif）与跨品类迁移性（transferability），用于后续趋势归组与产品开发。

严格要求：
- 仅输出一个合法的 JSON 对象，不要包含任何额外的解释或 Markdown 说明。
- JSON 必须包含以下全部字段：
  {{
    "is_graphic_print": true 或 false,
    "pattern_type": "graphic_print" | "solid" | "stripes" | "polka_dot" | "plaid" | "texture_only" | "uncertain",
    "confidence": 0.0 到 1.0 之间的小数,
    "filter_reason": "简短中文理由（25字以内，例如：纯色圆领打底衫，无印花图案 或 胸前含圣诞复古手绘插画与文字标语）",
    "print_subject": "核心印花主体中文描述（非印花则为空字符串 \\"\\"）",
    "slogan_text": "图片衣服上清晰可见的文字/英文标语 OCR（无文字则为空字符串 \\"\\"）",
    "visual_style": "简短中文视觉风格（如：复古做旧、卡通手绘、极简文字、美式学院、节日趣味；非印花为空字符串 \\"\\"）",
    "theme_tags": ["2到4个规范化小写英文或简短主题短语，不要包含 t-shirt/sweatshirt/shirt 等服装品类名；非印花则为空数组 []"],
    "season_tag": "必须严格从以下枚举中选 1 个：{' | '.join(SEASON_TAGS)}",
    "motif": "必须严格从以下受控词表中选 1 个：{' | '.join(MOTIF_VOCABULARY)}",
    "transferability": "必须严格从以下枚举中选 1 个：{' | '.join(TRANSFERABILITY_TYPES)}"
  }}
- 枚举填写指引：
  - season_tag：无明显季节限定选 "evergreen"；圣诞/感恩节/万圣节/情人节/复活节/夏季/开学季选对应项；其他特定节日选 "other_seasonal"。
  - motif：仅允许从上述受控词表精确选择 1 个最核心的图案主体；若纯文字无图形选 "text_only"；若词表确实无法覆盖或非印花选 "other"。
  - transferability：
    - "pure_graphic"：图案本身可自由迁移到 T 恤、卫衣、帆布包、杯子等多种载体；
    - "text_pun"：以文字标语/谐音梗为核心卖点，换字或改版同样易于迁移；
    - "form_dependent"：强依赖特定服装版型、拼接条纹、刺绣亮片工艺或全身满印结构，不易直接平面迁移。
- 判定原则：
  - 纯色基础款、多件装纯色衣、纯罗纹/华夫格面料 -> is_graphic_print=false, pattern_type="solid" 或 "texture_only"
  - 仅有普通横/竖条纹、普通波点、普通格纹且无额外独立印花图案 -> is_graphic_print=false, pattern_type="stripes" / "polka_dot" / "plaid"
  - 仅在袖口或胸口有极小纯品牌字母 Logo（如基础款素衫）且无主题图案 -> 若无印花设计价值可标为 solid 或 uncertain
  - 胸前/背部有明确插画、节日主题、动物/植物图案、幽默文字梗、刺绣/贴布图案 -> is_graphic_print=true, pattern_type="graphic_print"
"""

CLUSTER_SYSTEM_PROMPT = """你是一位资深的 Amazon 服饰印花选品总监。
你会收到一批高潜力印花服饰商品的精简 JSON（含 ASIN、印花主体、衣服标语 OCR、视觉风格、主题标签、04 潜力分、30 天估算销量与所属类目）。
请将这批商品归纳为若干具有商业二创价值的「印花趋势方向（Trend Clusters）」。

严格规则：
1. 允许「一图多组（Multi-Label）」：同一 ASIN 如果同时具备多个明确趋势维度（例如既是“感恩节热点”又是“宠物幽默梗”），可以同时出现在 1~3 个相关趋势分组中。
2. 严禁「垃圾桶分组」：绝对禁止创建“其他”、“其它”、“杂项”、“未分类”、“混合”、“Other”、“Misc”等兜底分组！如果某件商品设计独特、与任何其他商品无法构成 >=2 件的明确共同趋势，请直接留空不要放进任何分组（系统会自动将其作为「独立爆款单品」单独推荐）。
3. 每个趋势分组必须至少包含 2 个来自输入列表的有效 ASIN，主题聚焦、可直接交付设计师二创。
4. 输出严格为单个 JSON 对象，结构如下：
{
  "clusters": [
    {
      "name": "简短精准的中文趋势方向名（不超过24字，如：复古圣诞幽默文字梗）",
      "style_tags": ["风格标签1", "风格标签2", "风格标签3"],
      "target_audience": "目标受众（如：年轻女性 / 节日派对人群 / 宠物爱好者）",
      "notes": "简短趋势依据与二创切入点（说明共同视觉元素、文案结构或跨类目潜力）",
      "variant_suggestions": [
        "变体1（构图角度）：简短可执行二创测试方向",
        "变体2（配色/风格角度）：简短可执行二创测试方向",
        "变体3（文案/受众角度）：简短可执行二创测试方向"
      ],
      "asins": ["B0XXXXXXX1", "B0XXXXXXX2"]
    }
  ]
}
"""

VARIANTS_SYSTEM_PROMPT = """你是一位资深的 POD（按需印刷）跨境服饰印花产品经理与资深平面设计师。
你的任务是根据给定的印花方向主题、风格标签、受众与参考爆款商品，输出 3~5 条可直接交付设计师快速出图测款的「二创变体建议（variant_suggestions）」。

严格要求：
1. 必须覆盖不同二创切入角度：如【构图重构】、【配色/肌理替换】、【文案/标语延展】、【细分受众拓展】或【跨载体迁移】。
2. 每条建议简短具体、可直接执行（每条 15~55 字），严禁空泛套话（如“优化设计”“提升质感”）。
3. 仅输出一个合法的 JSON 对象，不要包含额外说明：
{
  "variant_suggestions": [
    "【构图】...",
    "【配色】...",
    "【文案】...",
    "【受众】..."
  ]
}
"""


class AIConfigError(ValueError):
    """Raised when AI configuration is missing or invalid."""


class AIRequestError(RuntimeError):
    """Raised when an upstream LLM API request fails."""


class AIResponseParseError(ValueError):
    """Raised when LLM output cannot be parsed into valid JSON."""


def compute_evidence_hash(title: Optional[str], main_image_url: Optional[str]) -> str:
    """Deterministic hash of `title + main_image_url` for cache invalidation."""
    norm_title = " ".join((title or "").strip().lower().split())
    norm_img = (main_image_url or "").strip()
    raw = f"{norm_title}|{norm_img}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def mask_api_key(key: Optional[str]) -> str:
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    if len(text) <= 8:
        return f"****{text[-4:]}"
    return f"{text[:3]}****{text[-4:]}"


def _clean_base_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return DEFAULT_AI_BASE_URL
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        raise AIConfigError("API Base URL 必须以 http:// 或 https:// 开头。")
    return cleaned


def _extract_balanced_json_fragment(text: str) -> Optional[str]:
    """Find the first balanced `{...}` or `[...]` block inside arbitrary prose."""
    start_idx = -1
    open_ch = ""
    close_ch = ""
    for i, ch in enumerate(text):
        if ch in ("{", "["):
            start_idx = i
            open_ch = ch
            close_ch = "}" if ch == "{" else "]"
            break
    if start_idx < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for j in range(start_idx, len(text)):
        ch = text[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx : j + 1]
    return None


def parse_llm_json_any(raw_text: Any) -> Any:
    """Fault-tolerant JSON extractor accepting either a JSON object or JSON array."""
    if isinstance(raw_text, (dict, list)):
        return raw_text
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AIResponseParseError("模型返回内容为空，无法解析 JSON。")

    text = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidates: List[str] = []
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    candidates.append(text)

    fragment = _extract_balanced_json_fragment(text)
    if fragment:
        candidates.append(fragment)

    for cand in candidates:
        if not cand:
            continue
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                parsed = json.loads(attempt)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except json.JSONDecodeError:
                continue

    raise AIResponseParseError(f"模型返回非合法 JSON：{text[:180]}")


def parse_llm_json(raw_text: Any) -> Dict[str, Any]:
    """Fault-tolerant JSON extractor for LLM completions requiring a top-level dict."""
    parsed = parse_llm_json_any(raw_text)
    if isinstance(parsed, dict):
        return parsed
    raise AIResponseParseError(f"模型返回非合法 JSON 对象：{str(raw_text)[:160]}")


def _extract_raw_clusters_list(parsed: Any) -> List[Any]:
    """Extract cluster objects from dict (multiple key aliases) or top-level JSON array."""
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    for key in ("clusters", "directions", "trend_clusters", "groups", "themes", "categories", "data", "result"):
        val = parsed.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            nested = _extract_raw_clusters_list(val)
            if nested:
                return nested
    # Fallback: find first list of dicts that look like cluster objects
    for val in parsed.values():
        if isinstance(val, list) and any(
            isinstance(x, dict) and ("asins" in x or "asin_list" in x or "name" in x)
            for x in val
        ):
            return val
    return []


def _coerce_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "yes", "1", "是"):
            return True
        if low in ("false", "no", "0", "否"):
            return False
    return default


THEME_TAG_EDGE_PUNCT_RE = re.compile(
    r"""^[\s,.;:!?#"'()\[\]{}<>/\\|~`^&*+=—–\-·•。，、；：！？【】（）《》“”‘’]+|[\s,.;:!?#"'()\[\]{}<>/\\|~`^&*+=—–\-·•。，、；：！？【】（）《》“”‘’]+$"""
)

THEME_TAG_STOPWORDS = frozenset(
    {
        "t-shirt",
        "tshirt",
        "t shirt",
        "tee",
        "tees",
        "shirt",
        "shirts",
        "sweatshirt",
        "sweatshirts",
        "hoodie",
        "hoodies",
        "pullover",
        "pullovers",
        "top",
        "tops",
        "apparel",
        "clothing",
        "clothes",
        "wear",
        "crewneck",
        "crew neck",
        "long sleeve",
        "short sleeve",
        "mens",
        "men",
        "womens",
        "women",
        "unisex",
        "adult",
        "graphic",
        "graphics",
        "print",
        "prints",
        "printed",
        "graphic tee",
        "graphic t-shirt",
        "graphic shirt",
        "graphic sweatshirt",
        "funny shirt",
        "funny tee",
        "funny t-shirt",
        "casual",
        "fashion",
        "gift",
        "gifts",
        "outfit",
        "style",
        "design",
        "pattern",
    }
)

THEME_TAG_GARMENT_SUFFIX_RE = re.compile(
    r"\s+(?:t-?shirts?|tees?|shirts?|sweatshirts?|hoodies?|pullovers?|tops?|apparel|clothing|crewneck)$",
    re.IGNORECASE,
)

THEME_TAG_SYNONYMS: Dict[str, str] = {
    "xmas": "christmas",
    "x-mas": "christmas",
    "christmas day": "christmas",
    "merry christmas": "christmas",
    "t-day": "thanksgiving",
    "thanksgiving day": "thanksgiving",
    "v-day": "valentines",
    "valentine": "valentines",
    "valentine's": "valentines",
    "valentine's day": "valentines",
    "valentines day": "valentines",
    "b2s": "back to school",
    "puppy": "dog",
    "puppies": "dog",
    "doggo": "dog",
    "doggos": "dog",
    "dogs": "dog",
    "kitty": "cat",
    "kitties": "cat",
    "kitten": "cat",
    "kittens": "cat",
    "cats": "cat",
    "dino": "dinosaur",
    "dinos": "dinosaur",
    "dinosaurs": "dinosaur",
    "flowers": "floral",
    "flower": "floral",
    "snowmen": "snowman",
    "bunnies": "bunny",
    "butterflies": "butterfly",
    "witches": "witch",
    "pumpkins": "pumpkin",
    "turkeys": "turkey",
    "ghosts": "ghost",
    "skulls": "skull",
    "skeletons": "skeleton",
    "mushrooms": "mushroom",
    "frogs": "frog",
    "ducks": "duck",
    "cows": "cow",
    "bears": "bear",
    "hearts": "heart",
    "reindeers": "reindeer",
    "books": "book",
    "teachers": "teacher",
    "nurses": "nurse",
    "cowboys": "cowboy",
    "retro style": "retro",
    "vintage style": "vintage",
}

SINGULAR_PROTECTED = frozenset(
    {
        "christmas",
        "valentines",
        "thanksgiving",
        "sports",
        "cactus",
        "status",
        "series",
        "species",
        "jesus",
        "dress",
        "glass",
        "grass",
        "boss",
        "chess",
        "princess",
        "fitness",
        "business",
        "happiness",
        "plus",
        "campus",
        "focus",
        "bonus",
        "chaos",
        "cosmos",
        "texas",
        "canvas",
        "mens",
        "womens",
        "paris",
        "tennis",
        "iris",
        "lotus",
        "hibiscus",
        "octopuses",
    }
)


def _singularize_english_word(word: str) -> str:
    """Lightweight deterministic English singularization for ASCII words."""
    if not word or not word.isascii() or not word.isalpha():
        return word
    if len(word) < 4 or word in SINGULAR_PROTECTED:
        return word
    if word in THEME_TAG_SYNONYMS:
        return THEME_TAG_SYNONYMS[word]
    if word.endswith("ies") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "zes")) and len(word) >= 5:
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "os")):
        return word[:-1]
    return word


def normalize_theme_tags(raw: Any, *, max_tags: int = 4) -> List[str]:
    """Standardize theme_tags: lowercase, strip punctuation, singularize, synonym-map, drop garment noise."""
    tokens: List[str] = []
    if isinstance(raw, list):
        tokens = [str(x).strip() for x in raw if x is not None and str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        tokens = [p.strip() for p in re.split(r"[,，、;/|\n]+", raw) if p.strip()]

    seen = set()
    out: List[str] = []
    for raw_tag in tokens:
        t = raw_tag.strip().lower()
        t = THEME_TAG_EDGE_PUNCT_RE.sub("", t).strip()
        t = re.sub(r"\s+", " ", t)
        if not t:
            continue

        # Drop pure garment/SEO noise words before & after suffix stripping
        if t in THEME_TAG_STOPWORDS:
            continue
        t = THEME_TAG_GARMENT_SUFFIX_RE.sub("", t).strip()
        t = THEME_TAG_EDGE_PUNCT_RE.sub("", t).strip()
        if not t or t in THEME_TAG_STOPWORDS:
            continue

        # Whole-phrase synonym mapping first
        if t in THEME_TAG_SYNONYMS:
            t = THEME_TAG_SYNONYMS[t]
        elif t.isascii():
            # Word-level synonym mapping + basic singularization for English phrases
            words = []
            for w in t.split(" "):
                w_clean = THEME_TAG_EDGE_PUNCT_RE.sub("", w)
                if not w_clean:
                    continue
                if w_clean in THEME_TAG_SYNONYMS:
                    w_clean = THEME_TAG_SYNONYMS[w_clean]
                else:
                    w_clean = _singularize_english_word(w_clean)
                    if w_clean in THEME_TAG_SYNONYMS:
                        w_clean = THEME_TAG_SYNONYMS[w_clean]
                if w_clean and w_clean not in THEME_TAG_STOPWORDS:
                    words.append(w_clean)
            t = " ".join(words).strip()
            if t in THEME_TAG_SYNONYMS:
                t = THEME_TAG_SYNONYMS[t]

        t = t[:32].strip()
        if not t or t in THEME_TAG_STOPWORDS:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= max_tags:
            break
    return out


def _normalize_theme_tags(raw: Any) -> List[str]:
    return normalize_theme_tags(raw)


def normalize_variant_suggestions(raw: Any, *, max_items: int = 5) -> List[str]:
    """Normalize 3~5 actionable POD remix variant suggestions into a clean unique string list."""
    if raw is None:
        return []
    candidates: List[str] = []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return normalize_variant_suggestions(parsed, max_items=max_items)
            except json.JSONDecodeError:
                pass
        for line in re.split(r"[\n;；]+", stripped):
            if line.strip():
                candidates.append(line.strip())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip())
            elif isinstance(item, dict):
                angle = str(item.get("angle") or item.get("dimension") or item.get("type") or "").strip()
                desc = str(
                    item.get("suggestion")
                    or item.get("idea")
                    or item.get("description")
                    or item.get("text")
                    or item.get("title")
                    or ""
                ).strip()
                if angle and desc:
                    candidates.append(f"【{angle}】{desc}")
                elif desc:
                    candidates.append(desc)
    else:
        return []

    seen = set()
    out: List[str] = []
    for text in candidates:
        cleaned = re.sub(r"^\s*(?:[-*•·]|\d+[.)、])\s*", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)[:120]
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _normalize_enum(raw: Any, allowed: Tuple[str, ...], fallback: str) -> str:
    """Validate an enum value against a strict whitelist with safe fallback."""
    if raw is None:
        return fallback
    cleaned = re.sub(r"[\s\-/]+", "_", str(raw).strip().lower()).strip("_")
    return cleaned if cleaned in allowed else fallback


def normalize_vision_result(
    parsed: Dict[str, Any],
    *,
    has_image: bool = True,
) -> Dict[str, Any]:
    """Normalize and validate single-product vision v2 JSON into the unified contract."""
    raw_pattern = str(parsed.get("pattern_type") or "uncertain").strip().lower()
    if raw_pattern not in PATTERN_TYPES:
        raw_pattern = "uncertain"

    if "is_graphic_print" in parsed:
        is_print = _coerce_bool(parsed.get("is_graphic_print"), default=(raw_pattern == "graphic_print"))
    else:
        is_print = raw_pattern == "graphic_print"

    # Keep pattern_type consistent with is_graphic_print when clearly contradictory
    if is_print and raw_pattern in ("solid", "stripes", "polka_dot", "plaid", "texture_only"):
        is_print = False
    elif not is_print and raw_pattern == "graphic_print":
        raw_pattern = "uncertain"

    try:
        conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, round(conf, 3)))
    if not has_image:
        conf = min(conf, 0.65)

    filter_reason = str(parsed.get("filter_reason") or "").strip()[:200]
    if not filter_reason:
        filter_reason = (
            "识别为有效印花/图案设计"
            if is_print
            else f"视觉判定为{PATTERN_TYPE_LABELS.get(raw_pattern, raw_pattern)}"
        )

    print_subject = str(parsed.get("print_subject") or "").strip()[:160] if is_print else ""
    slogan_text = str(parsed.get("slogan_text") or "").strip()[:200] if is_print else ""
    visual_style = str(parsed.get("visual_style") or "").strip()[:80] if is_print else ""
    theme_tags = normalize_theme_tags(parsed.get("theme_tags")) if is_print else []

    season_tag = _normalize_enum(parsed.get("season_tag"), SEASON_TAGS, "evergreen")
    motif = _normalize_enum(parsed.get("motif"), MOTIF_VOCABULARY, "other")
    transferability = _normalize_enum(
        parsed.get("transferability"), TRANSFERABILITY_TYPES, "pure_graphic"
    )

    suggest_exclude = (not is_print) and (conf >= AI_SUGGEST_EXCLUDE_CONFIDENCE)
    return {
        "prompt_version": VISION_PROMPT_VERSION,
        "is_graphic_print": is_print,
        "pattern_type": raw_pattern,
        "pattern_label": PATTERN_TYPE_LABELS.get(raw_pattern, raw_pattern),
        "confidence": conf,
        "filter_reason": filter_reason,
        "print_subject": print_subject,
        "slogan_text": slogan_text,
        "visual_style": visual_style,
        "theme_tags": theme_tags,
        "season_tag": season_tag,
        "season_label": SEASON_TAG_LABELS.get(season_tag, season_tag),
        "motif": motif,
        "motif_label": MOTIF_LABELS.get(motif, motif),
        "transferability": transferability,
        "transferability_label": TRANSFERABILITY_LABELS.get(transferability, transferability),
        "needs_v2_upgrade": False,
        "suggest_exclude": suggest_exclude,
    }


def is_forbidden_cluster_name(name: str) -> bool:
    """Reject generic catch-all / misc cluster names like '其他' or '杂项'."""
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    if not cleaned:
        return True
    low = cleaned.lower()
    if low in FORBIDDEN_CLUSTER_EXACT:
        return True
    for pat in FORBIDDEN_CLUSTER_PATTERNS:
        if pat.search(cleaned):
            return True
    return False


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in (model or "").strip().lower()


def build_vision_request_payload(
    product: Dict[str, Any],
    *,
    model: str = DEFAULT_AI_MODEL,
    include_response_format: bool = True,
) -> Dict[str, Any]:
    """Build standard OpenAI Chat Completions multimodal body (`image_url` with `detail: low`)."""
    title = (product.get("title") or "").strip()
    brand = (product.get("brand") or "").strip() or "未知"
    image_url = (product.get("main_image_url") or product.get("image_url") or "").strip()

    user_text = (
        f"请分析以下 Amazon 服饰商品：\n"
        f"- ASIN: {product.get('asin', '')}\n"
        f"- 品牌: {brand}\n"
        f"- 标题: {title or '（无标题）'}\n"
        f"请严格按系统提示返回单个 JSON 对象。"
    )

    if image_url:
        user_content: Any = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": image_url,
                    "detail": "low",
                },
            },
        ]
    else:
        user_content = user_text + "\n（注意：该商品缺少主图，请谨慎给出较低置信度。）"

    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    if _is_deepseek_model(model):
        # Disable DeepSeek thinking mode so reasoning_content never consumes max_tokens
        payload["thinking"] = {"type": "disabled"}
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def build_cluster_request_payload(
    compact_items: List[Dict[str, Any]],
    *,
    model: str = DEFAULT_AI_MODEL,
    include_response_format: bool = True,
) -> Dict[str, Any]:
    """Build text-mode Reduce prompt payload for Stage 05 trend clustering."""
    user_text = (
        f"以下是 {len(compact_items)} 件高潜力印花候选商品的精简结构化数据（JSON 数组）。"
        f"请遵循系统规则进行多标签趋势归组，不要创建“其他/杂项”分组，仅输出 JSON 对象：\n\n"
        + json.dumps(compact_items, ensure_ascii=False, separators=(",", ":"))
    )
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": CLUSTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    if _is_deepseek_model(model):
        # Critical for deepseek-flash: thinking mode is enabled by default and counts against
        # max_tokens, which can exhaust all tokens in reasoning_content leaving content empty.
        payload["thinking"] = {"type": "disabled"}
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def build_variants_request_payload(
    direction_info: Dict[str, Any],
    *,
    model: str = DEFAULT_AI_MODEL,
    include_response_format: bool = True,
) -> Dict[str, Any]:
    """Build text-mode prompt payload to generate 3~5 testable POD remix variants for a direction."""
    compact_prods = []
    for p in (direction_info.get("linked_products") or [])[:8]:
        ai = p.get("ai_analysis") or {}
        compact_prods.append(
            {
                "asin": p.get("asin"),
                "title": (p.get("title") or "")[:72],
                "sales_30d": p.get("listing_sales_30d"),
                "price": p.get("price"),
                "print_subject": (ai.get("print_subject") or "")[:80],
                "slogan_text": (ai.get("slogan_text") or "")[:80],
                "motif": ai.get("motif") or "",
                "season_tag": ai.get("season_tag") or "",
                "transferability": ai.get("transferability") or "",
            }
        )
    context_obj = {
        "name": direction_info.get("name") or "",
        "direction_type": direction_info.get("direction_type") or "trend_cluster",
        "season_tag": direction_info.get("season_tag") or "evergreen",
        "style_tags": direction_info.get("style_tags") or [],
        "target_audience": direction_info.get("target_audience") or "",
        "notes": (direction_info.get("notes") or "")[:400],
        "reference_products": compact_prods,
    }
    user_text = (
        "请基于以下印花方向与参考爆款数据，生成 3~5 条覆盖不同切入角度（构图/配色/文案/受众）的可测试 POD 二创变体建议，仅返回 JSON 对象：\n\n"
        + json.dumps(context_obj, ensure_ascii=False, separators=(",", ":"))
    )
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.35,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": VARIANTS_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    if _is_deepseek_model(model):
        payload["thinking"] = {"type": "disabled"}
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def default_http_requester(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: float = 45.0,
) -> Tuple[int, Dict[str, Any]]:
    """Pure stdlib `urllib.request` JSON POST client for OpenAI-compatible APIs."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AIRequestError(f"AI 服务端返回非 JSON 内容（HTTP {status}）。") from exc
            return status, parsed
    except urllib_error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            err_body = str(exc.reason)
        try:
            err_json = json.loads(err_body)
            if isinstance(err_json, dict):
                return int(exc.code), err_json
        except Exception:
            pass
        return int(exc.code), {"error": {"message": err_body or f"HTTP {exc.code}"}}
    except urllib_error.URLError as exc:
        raise AIRequestError(f"无法连接 AI 服务：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AIRequestError("AI 接口请求超时，请稍后重试。") from exc
    except OSError as exc:
        raise AIRequestError(f"网络请求异常：{exc}") from exc


def _extract_assistant_content(api_response: Dict[str, Any]) -> str:
    choices = api_response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIResponseParseError("模型响应中缺少 choices 字段。")
    first = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(first.get("finish_reason") or "").strip().lower()
    msg = first.get("message")
    if not isinstance(msg, dict):
        raise AIResponseParseError("模型响应中缺少 message 对象。")
    content = msg.get("content")
    if isinstance(content, list):
        parts = [str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"]
        content = "\n".join(parts)
    if not isinstance(content, str) or not content.strip():
        if finish_reason == "length":
            raise AIResponseParseError("模型输出超过长度上限（finish_reason=length），建议缩小范围后重试。")
        raise AIResponseParseError("模型响应 message.content 为空或格式不受支持。")
    return content


def _get_finish_reason(api_response: Dict[str, Any]) -> str:
    choices = api_response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "").strip().lower()
    return ""


class AIService:
    """Unified DeepSeek Vision & Clustering service with SQLite cache and local secret storage."""

    def __init__(
        self,
        *,
        app_db_path: Path = DEFAULT_APP_DB_PATH,
        config_path: Path = WORKBENCH_AI_CONFIG_PATH,
        http_requester: Optional[
            Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, Dict[str, Any]]]
        ] = None,
        retry_delay: float = 0.8,
        timeout: float = 45.0,
    ):
        self.app_db_path = Path(app_db_path)
        self.config_path = Path(config_path)
        self.http_requester = http_requester or default_http_requester
        self.retry_delay = float(retry_delay)
        self.timeout = float(timeout)
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
                conn.executescript(AI_CACHE_SCHEMA_SQL)
                # Idempotent migration for pre-existing v1 `product_ai_cache` tables
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(product_ai_cache)").fetchall()
                }
                if "prompt_version" not in cols:
                    conn.execute(
                        "ALTER TABLE product_ai_cache ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'v1'"
                    )
                if "season_tag" not in cols:
                    conn.execute(
                        "ALTER TABLE product_ai_cache ADD COLUMN season_tag TEXT NOT NULL DEFAULT ''"
                    )
                if "motif" not in cols:
                    conn.execute(
                        "ALTER TABLE product_ai_cache ADD COLUMN motif TEXT NOT NULL DEFAULT ''"
                    )
                if "transferability" not in cols:
                    conn.execute(
                        "ALTER TABLE product_ai_cache ADD COLUMN transferability TEXT NOT NULL DEFAULT ''"
                    )
                conn.commit()

    # ------------------------------------------------------------------
    # Local Secret Configuration (`data/ai_config.local`)
    # ------------------------------------------------------------------
    def _read_raw_config(self) -> Dict[str, str]:
        cfg = {
            "base_url": DEFAULT_AI_BASE_URL,
            "model": DEFAULT_AI_MODEL,
            "api_key": "",
        }
        if not self.config_path.exists():
            return cfg
        try:
            raw = self.config_path.read_text(encoding="utf-8").strip()
            if not raw:
                return cfg
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                if parsed.get("base_url"):
                    cfg["base_url"] = str(parsed["base_url"]).strip().rstrip("/")
                if parsed.get("model"):
                    cfg["model"] = str(parsed["model"]).strip()
                if parsed.get("api_key"):
                    cfg["api_key"] = str(parsed["api_key"]).strip()
        except Exception:
            pass
        return cfg

    def _write_raw_config(self, cfg: Dict[str, str]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def get_config_status(self) -> Dict[str, Any]:
        """Return sanitized AI config status (NEVER exposes plaintext api_key)."""
        cfg = self._read_raw_config()
        has_key = bool(cfg["api_key"])
        return {
            "configured": has_key,
            "has_api_key": has_key,
            "api_key_masked": mask_api_key(cfg["api_key"]),
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "default_base_url": DEFAULT_AI_BASE_URL,
            "default_model": DEFAULT_AI_MODEL,
            "config_file_label": "data/ai_config.local（已忽略提交）",
        }

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise AIConfigError("配置请求体必须为 JSON 对象。")

        with self._lock:
            current = self._read_raw_config()
            base_url = _clean_base_url(
                payload["base_url"] if "base_url" in payload and payload["base_url"] is not None else current["base_url"]
            )
            model = str(
                payload["model"] if "model" in payload and payload["model"] is not None else current["model"]
            ).strip()
            if not model:
                model = DEFAULT_AI_MODEL
            if len(model) > 120:
                raise AIConfigError("模型名称长度不能超过 120 字符。")

            api_key = current["api_key"]
            if payload.get("clear_key") is True or payload.get("action") == "clear":
                api_key = ""
            elif "api_key" in payload:
                new_key = str(payload.get("api_key") or "").strip()
                if new_key:
                    if len(new_key) < 6:
                        raise AIConfigError("API Key 长度过短，请检查是否完整粘贴。")
                    api_key = new_key

            self._write_raw_config({"base_url": base_url, "model": model, "api_key": api_key})

        return {
            "saved": True,
            "config": self.get_config_status(),
        }

    def clear_api_key(self) -> Dict[str, Any]:
        with self._lock:
            current = self._read_raw_config()
            current["api_key"] = ""
            self._write_raw_config(current)
        return {
            "cleared": True,
            "config": self.get_config_status(),
        }

    def _require_ready_config(self) -> Dict[str, str]:
        cfg = self._read_raw_config()
        if not cfg["api_key"]:
            raise AIConfigError("尚未配置 DeepSeek API Key，请先点击「AI 配置」填入密钥后再执行。")
        return cfg

    def _call_chat_completions(
        self,
        payload: Dict[str, Any],
        *,
        cfg: Optional[Dict[str, str]] = None,
        max_retries: int = 2,
        timeout_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        active = cfg or self._require_ready_config()
        endpoint = f"{active['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {active['api_key']}",
        }
        eff_timeout = float(timeout_override) if timeout_override is not None else self.timeout

        req_payload = dict(payload)
        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                status, data = self.http_requester(endpoint, headers, req_payload, eff_timeout)
            except AIRequestError as exc:
                last_err = exc
                if attempt < max_retries:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                raise

            if 200 <= status < 300:
                return data

            err_msg = ""
            if isinstance(data, dict):
                err_obj = data.get("error")
                if isinstance(err_obj, dict):
                    err_msg = str(err_obj.get("message") or err_obj)
                elif err_obj:
                    err_msg = str(err_obj)
            err_msg = err_msg or f"HTTP {status}"

            # Graceful fallback when a compatible provider rejects `response_format` or `thinking`
            if status == 400 and "response_format" in req_payload and "response_format" in err_msg.lower():
                req_payload = {k: v for k, v in req_payload.items() if k != "response_format"}
                continue
            if status == 400 and "thinking" in req_payload and "thinking" in err_msg.lower():
                req_payload = {k: v for k, v in req_payload.items() if k != "thinking"}
                continue

            # Retry on rate limit or transient server errors
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(self.retry_delay * (attempt + 1))
                continue

            if status in (401, 403):
                raise AIRequestError(f"AI 鉴权失败（HTTP {status}），请检查 API Key 是否有效。")
            raise AIRequestError(f"AI 接口返回错误（HTTP {status}）：{err_msg[:220]}")

        raise last_err or AIRequestError("AI 接口请求失败。")

    def test_connection(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Lightweight connectivity & auth verification using minimal tokens."""
        cfg = self._read_raw_config()
        if isinstance(overrides, dict):
            if overrides.get("base_url"):
                cfg["base_url"] = _clean_base_url(str(overrides["base_url"]))
            if overrides.get("model"):
                cfg["model"] = str(overrides["model"]).strip() or cfg["model"]
            if overrides.get("api_key"):
                cfg["api_key"] = str(overrides["api_key"]).strip()
        if not cfg["api_key"]:
            raise AIConfigError("请先填写 API Key 再测试连通性。")

        t0 = time.monotonic()
        payload: Dict[str, Any] = {
            "model": cfg["model"],
            "temperature": 0,
            "max_tokens": 32,
            "messages": [
                {"role": "user", "content": "Reply with OK"},
            ],
        }
        if _is_deepseek_model(cfg["model"]):
            payload["thinking"] = {"type": "disabled"}
        resp = self._call_chat_completions(payload, cfg=cfg, max_retries=0)
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        reply = _extract_assistant_content(resp).strip()[:40]
        return {
            "ok": True,
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "latency_ms": latency_ms,
            "reply_preview": reply,
            "message": f"连通成功（{cfg['model']} · {latency_ms} ms）",
        }

    # ------------------------------------------------------------------
    # SQLite Cache Operations (`product_ai_cache`)
    # ------------------------------------------------------------------
    def _row_to_cache_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        is_print = bool(row["is_graphic_print"])
        conf = float(row["confidence"])
        pattern_type = row["pattern_type"]
        row_keys = set(row.keys())
        prompt_version = (
            str(row["prompt_version"] or "v1").strip()
            if "prompt_version" in row_keys
            else "v1"
        ) or "v1"
        season_tag = str(row["season_tag"] or "").strip() if "season_tag" in row_keys else ""
        motif = str(row["motif"] or "").strip() if "motif" in row_keys else ""
        transferability = (
            str(row["transferability"] or "").strip() if "transferability" in row_keys else ""
        )
        return {
            "asin": row["asin"],
            "evidence_hash": row["evidence_hash"],
            "model": row["model"],
            "prompt_version": prompt_version,
            "needs_v2_upgrade": prompt_version != VISION_PROMPT_VERSION,
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

    def get_cache_map(self, asins: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            if asins is None:
                rows = conn.execute("SELECT * FROM product_ai_cache").fetchall()
            else:
                clean = [str(a).strip().upper() for a in asins if a]
                if not clean:
                    return {}
                placeholders = ",".join("?" for _ in clean)
                rows = conn.execute(
                    f"SELECT * FROM product_ai_cache WHERE asin IN ({placeholders})",
                    clean,
                ).fetchall()
        return {r["asin"]: self._row_to_cache_dict(r) for r in rows}

    def get_valid_cached_result(
        self,
        product: Dict[str, Any],
        *,
        required_version: str = VISION_PROMPT_VERSION,
    ) -> Optional[Dict[str, Any]]:
        """Return cached entry ONLY when both `evidence_hash` AND `prompt_version` match."""
        asin = str(product.get("asin") or "").strip().upper()
        if not asin:
            return None
        ev_hash = compute_evidence_hash(
            product.get("title"),
            product.get("main_image_url") or product.get("image_url"),
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM product_ai_cache WHERE asin = ?",
                (asin,),
            ).fetchone()
        if row is None or row["evidence_hash"] != ev_hash:
            return None
        parsed = self._row_to_cache_dict(row)
        if required_version and parsed.get("prompt_version") != required_version:
            return None
        return parsed

    def _save_cache_result(
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
        season_tag = str(normalized.get("season_tag") or "").strip()
        motif = str(normalized.get("motif") or "").strip()
        transferability = str(normalized.get("transferability") or "").strip()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO product_ai_cache
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
                        season_tag,
                        motif,
                        transferability,
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
            "needs_v2_upgrade": pv != VISION_PROMPT_VERSION,
            "updated_at": now,
            "cached": True,
        }

    # ------------------------------------------------------------------
    # Single-Product Vision Analysis (Unified for Stage 02 & Stage 05)
    # ------------------------------------------------------------------
    def analyze_product_image(
        self,
        product: Dict[str, Any],
        *,
        force: bool = False,
    ) -> Tuple[Dict[str, Any], bool]:
        """Analyze one product with `deepseek-flash` multimodal API, with permanent SQLite cache.

        Returns `(result_dict, from_cache)`.
        """
        asin = str(product.get("asin") or "").strip().upper()
        if not asin:
            raise ValueError("商品缺少有效 ASIN。")

        image_url = (product.get("main_image_url") or product.get("image_url") or "").strip()
        ev_hash = compute_evidence_hash(product.get("title"), image_url)

        if not force:
            cached = self.get_valid_cached_result(product)
            if cached is not None:
                return cached, True

        cfg = self._require_ready_config()
        payload = build_vision_request_payload(product, model=cfg["model"])
        resp = self._call_chat_completions(payload, cfg=cfg)
        content = _extract_assistant_content(resp)
        parsed = parse_llm_json(content)
        normalized = normalize_vision_result(parsed, has_image=bool(image_url))
        saved = self._save_cache_result(
            asin=asin,
            evidence_hash=ev_hash,
            model=cfg["model"],
            normalized=normalized,
            raw_obj=parsed,
        )
        return saved, False

    def split_cached_and_uncached_detailed(
        self,
        products: List[Dict[str, Any]],
        *,
        required_version: str = VISION_PROMPT_VERSION,
    ) -> Tuple[
        List[Tuple[Dict[str, Any], Dict[str, Any]]],
        List[Dict[str, Any]],
        List[Dict[str, Any]],
    ]:
        """Partition products into `(v2_cached_pairs, brand_new_uncached, v1_upgrade_list)`.

        - `v2_cached_pairs`: `evidence_hash` matches AND `prompt_version == required_version`.
        - `v1_upgrade_list`: `evidence_hash` matches BUT `prompt_version != required_version` (legacy v1).
        - `brand_new_uncached`: no cache row OR `evidence_hash` changed.
        """
        asins = [str(p.get("asin") or "").strip().upper() for p in products if p.get("asin")]
        cache_map = self.get_cache_map(asins)
        v2_cached_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        brand_new_uncached: List[Dict[str, Any]] = []
        v1_upgrade_list: List[Dict[str, Any]] = []
        for p in products:
            asin = str(p.get("asin") or "").strip().upper()
            if not asin:
                continue
            ev_hash = compute_evidence_hash(
                p.get("title"),
                p.get("main_image_url") or p.get("image_url"),
            )
            entry = cache_map.get(asin)
            if entry and entry["evidence_hash"] == ev_hash:
                if not required_version or entry.get("prompt_version") == required_version:
                    v2_cached_pairs.append((p, entry))
                else:
                    v1_upgrade_list.append(p)
            else:
                brand_new_uncached.append(p)
        return v2_cached_pairs, brand_new_uncached, v1_upgrade_list

    def split_cached_and_uncached(
        self, products: List[Dict[str, Any]]
    ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], List[Dict[str, Any]]]:
        """Partition products into `(v2_cached_pairs, needs_api_list)` using a single SQLite query."""
        v2_cached, brand_new, v1_upgrade = self.split_cached_and_uncached_detailed(products)
        return v2_cached, brand_new + v1_upgrade

    # ------------------------------------------------------------------
    # Stage 02 Batch Visual Purification (Plan A: Never Auto-Excludes)
    # ------------------------------------------------------------------
    DEFAULT_PURIFY_LIMIT = 50

    @staticmethod
    def _purify_heat_sort_key(prod: Dict[str, Any]) -> Tuple[Any, ...]:
        """Prioritize uncached items by heat: valid sales desc -> best source BSR asc -> main BSR asc -> ASIN."""
        raw_sales = prod.get("listing_sales_30d")
        has_sales = False
        sales_val = 0.0
        if raw_sales is not None:
            try:
                sales_val = float(raw_sales)
                has_sales = sales_val > 0
            except (TypeError, ValueError):
                pass

        best_src_bsr = 10**9
        for src in prod.get("source_categories") or []:
            if isinstance(src, dict):
                raw_b = src.get("source_category_bsr")
                if raw_b is not None:
                    try:
                        bi = int(raw_b)
                        if 0 < bi < best_src_bsr:
                            best_src_bsr = bi
                    except (TypeError, ValueError):
                        pass
        bsr_map = prod.get("category_bsr_by_source")
        if best_src_bsr == 10**9 and isinstance(bsr_map, dict):
            for entry in bsr_map.values():
                raw_b = entry.get("bsr") if isinstance(entry, dict) else entry
                if raw_b is not None:
                    try:
                        bi = int(raw_b)
                        if 0 < bi < best_src_bsr:
                            best_src_bsr = bi
                    except (TypeError, ValueError):
                        pass

        main_bsr = 10**9
        if prod.get("main_category_bsr") is not None:
            try:
                mb = int(prod["main_category_bsr"])
                if mb > 0:
                    main_bsr = mb
            except (TypeError, ValueError):
                pass

        return (
            0 if has_sales else 1,
            -sales_val if has_sales else 0.0,
            best_src_bsr,
            main_bsr,
            str(prod.get("asin") or ""),
        )

    def purify_products(
        self,
        products: List[Dict[str, Any]],
        *,
        dry_run: bool = False,
        max_workers: int = 8,
        limit: Any = DEFAULT_PURIFY_LIMIT,
        skip_suspected_non_print: bool = True,
    ) -> Dict[str, Any]:
        """Inspect `unreviewed` products via vision model and write `product_ai_cache`.

        - Automatically filters out products missing `main_image_url` / `image_url` (never wastes Vision API).
        - Optionally skips text-flagged `suspected_non_print` items (`skip_suspected_non_print=True` by default).
        - Prioritizes uncached items by heat (sales / within-category BSR) and caps batch size via `limit`
          (default 50; pass `0`, `-1`, or `"all"` to analyze all eligible uncached items).
        - NEVER modifies `manual_reviews` or auto-excludes any product.
        """
        cfg_status = self.get_config_status()
        raw_total = len(products or [])

        # 1) Hard skip products without any main image URL (never waste multimodal API tokens)
        with_image: List[Dict[str, Any]] = []
        skipped_missing_image_count = 0
        for p in products or []:
            img_url = str(p.get("main_image_url") or p.get("image_url") or "").strip()
            if not img_url:
                skipped_missing_image_count += 1
            else:
                with_image.append(p)

        # 2) Optionally skip items already pre-screened by text rules as `suspected_non_print`
        available_suspected_count = sum(
            1
            for p in with_image
            if str(p.get("automatic_classification") or "").strip() == "suspected_non_print"
        )
        eff_skip_suspected = _coerce_bool(skip_suspected_non_print, default=True)
        if eff_skip_suspected:
            eligible_products = [
                p
                for p in with_image
                if str(p.get("automatic_classification") or "").strip() != "suspected_non_print"
            ]
            skipped_suspected_count = len(with_image) - len(eligible_products)
        else:
            eligible_products = with_image
            skipped_suspected_count = 0

        # 3) Partition cached vs uncached, then sort uncached by heat priority (sales / BSR)
        cached_pairs, brand_new, v1_upgrade = self.split_cached_and_uncached_detailed(eligible_products)
        uncached = sorted(brand_new + v1_upgrade, key=self._purify_heat_sort_key)

        # 4) Parse batch limit (default 50; 0 / -1 / "all" means unlimited)
        eff_limit: Optional[int]
        if limit is None:
            eff_limit = self.DEFAULT_PURIFY_LIMIT
        elif isinstance(limit, str) and limit.strip().lower() in ("all", "unlimited", "0", "-1", ""):
            eff_limit = None
        else:
            parsed_lim = int(limit)
            eff_limit = parsed_lim if parsed_lim > 0 else None

        if eff_limit is not None:
            uncached_target = uncached[:eff_limit]
        else:
            uncached_target = uncached
        deferred_by_limit_count = max(0, len(uncached) - len(uncached_target))

        already_suggest_exclude = sum(1 for _, c in cached_pairs if c.get("suggest_exclude"))

        if dry_run:
            return {
                "dry_run": True,
                "prompt_version": VISION_PROMPT_VERSION,
                "raw_unreviewed_count": raw_total,
                "total_candidates": len(eligible_products),
                "skipped_missing_image_count": skipped_missing_image_count,
                "skipped_suspected_non_print_count": skipped_suspected_count,
                "available_suspected_non_print_count": available_suspected_count,
                "skip_suspected_non_print": eff_skip_suspected,
                "limit": eff_limit,
                "deferred_by_limit_count": deferred_by_limit_count,
                "cached_count": len(cached_pairs),
                "new_uncached_count": len(brand_new),
                "upgrade_v1_count": len(v1_upgrade),
                "uncached_count": len(uncached),
                "will_analyze_count": len(uncached_target),
                "already_suggest_exclude_count": already_suggest_exclude,
                "model": cfg_status["model"],
                "base_url": cfg_status["base_url"],
                "has_api_key": cfg_status["has_api_key"],
                "detail_mode": "low",
                "auto_exclude": False,
            }

        # Confirm API key is configured before firing any requests
        if uncached_target:
            self._require_ready_config()

        analyzed: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        if uncached_target:
            workers = max(1, min(int(max_workers or 8), 8, len(uncached_target)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self.analyze_product_image, prod, force=False): prod
                    for prod in uncached_target
                }
                for fut in as_completed(future_map):
                    prod = future_map[fut]
                    try:
                        res, _ = fut.result()
                        analyzed.append(res)
                    except Exception as exc:
                        errors.append(
                            {
                                "asin": str(prod.get("asin") or ""),
                                "error": str(exc)[:220],
                            }
                        )

        newly_suggest_exclude = sum(1 for r in analyzed if r.get("suggest_exclude"))
        newly_graphic_print = sum(1 for r in analyzed if r.get("is_graphic_print"))

        return {
            "dry_run": False,
            "prompt_version": VISION_PROMPT_VERSION,
            "raw_unreviewed_count": raw_total,
            "total_candidates": len(eligible_products),
            "skipped_missing_image_count": skipped_missing_image_count,
            "skipped_suspected_non_print_count": skipped_suspected_count,
            "skip_suspected_non_print": eff_skip_suspected,
            "limit": eff_limit,
            "deferred_by_limit_count": deferred_by_limit_count,
            "cached_skipped": len(cached_pairs),
            "new_uncached_count": len(brand_new),
            "upgrade_v1_count": len(v1_upgrade),
            "analyzed_count": len(analyzed),
            "failed_count": len(errors),
            "newly_suggest_exclude_count": newly_suggest_exclude,
            "newly_graphic_print_count": newly_graphic_print,
            "total_suggest_exclude_count": already_suggest_exclude + newly_suggest_exclude,
            "errors": errors[:10],
            "model": cfg_status["model"],
            "base_url": cfg_status["base_url"],
            "auto_exclude": False,
        }

    # ------------------------------------------------------------------
    # Stage 05 Map-Reduce Clustering (Multi-Label, No Misc/Other Bucket)
    # ------------------------------------------------------------------
    def cluster_candidates(
        self,
        candidates: List[Dict[str, Any]],
        *,
        dry_run: bool = False,
        max_workers: int = 8,
    ) -> Dict[str, Any]:
        """Map-Reduce clustering for Stage 05 top candidates.

        - Step 1 (Map): Reuse v2 `product_ai_cache`; call vision API ONLY for uncached or v1-upgrade items.
        - Step 2 (Reduce): 1 text-mode `deepseek-flash` call producing multi-label trend clusters,
          with strict server-side guard rejecting catch-all/misc buckets and <2 ASIN clusters.
        """
        cfg_status = self.get_config_status()
        cached_pairs, brand_new, v1_upgrade = self.split_cached_and_uncached_detailed(candidates)
        uncached = brand_new + v1_upgrade

        if dry_run:
            return {
                "dry_run": True,
                "prompt_version": VISION_PROMPT_VERSION,
                "candidate_count": len(candidates),
                "cached_count": len(cached_pairs),
                "new_uncached_count": len(brand_new),
                "upgrade_v1_count": len(v1_upgrade),
                "uncached_image_calls": len(uncached),
                "reduce_calls": 1 if len(candidates) >= 2 else 0,
                "total_api_calls": len(uncached) + (1 if len(candidates) >= 2 else 0),
                "model": cfg_status["model"],
                "base_url": cfg_status["base_url"],
                "has_api_key": cfg_status["has_api_key"],
            }

        cfg = self._require_ready_config()
        if len(candidates) < 2:
            return {
                "dry_run": False,
                "map_stats": {
                    "candidate_count": len(candidates),
                    "cached_hits": len(cached_pairs),
                    "image_calls_executed": 0,
                    "image_calls_failed": 0,
                },
                "clusters": [],
                "rejected_clusters": [],
                "model": cfg["model"],
            }

        # Step 1: Map — ensure visual features for every uncached candidate
        vision_by_asin: Dict[str, Dict[str, Any]] = {
            str(p["asin"]).upper(): entry for p, entry in cached_pairs
        }
        map_errors: List[Dict[str, str]] = []
        image_calls_executed = 0

        if uncached:
            workers = max(1, min(int(max_workers or 8), 8, len(uncached)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self.analyze_product_image, prod, force=False): prod
                    for prod in uncached
                }
                for fut in as_completed(future_map):
                    prod = future_map[fut]
                    asin = str(prod.get("asin") or "").upper()
                    try:
                        res, from_cache = fut.result()
                        vision_by_asin[asin] = res
                        if not from_cache:
                            image_calls_executed += 1
                    except Exception as exc:
                        map_errors.append({"asin": asin, "error": str(exc)[:200]})

        # Step 2: Reduce — build compact JSON & invoke 1 text-mode clustering call
        valid_asin_set = {str(p["asin"]).upper() for p in candidates if p.get("asin")}
        compact_items: List[Dict[str, Any]] = []
        for p in candidates:
            asin = str(p.get("asin") or "").upper()
            if not asin:
                continue
            v = vision_by_asin.get(asin) or {}
            item_obj: Dict[str, Any] = {
                "asin": asin,
                "title": (p.get("title") or "")[:72],
                "score_04": p.get("total_score"),
                "sales_30d": p.get("listing_sales_30d"),
                "categories": [
                    s.get("short_name") or s.get("source_category") or s.get("node_id")
                    for s in (p.get("source_categories") or [])
                ],
            }
            if v.get("print_subject"):
                item_obj["print_subject"] = v["print_subject"][:100]
            if v.get("slogan_text"):
                item_obj["slogan_text"] = v["slogan_text"][:100]
            if v.get("visual_style"):
                item_obj["visual_style"] = v["visual_style"][:40]
            if v.get("theme_tags"):
                item_obj["theme_tags"] = v["theme_tags"][:4]
            if v.get("season_tag"):
                item_obj["season_tag"] = v["season_tag"]
            if v.get("motif"):
                item_obj["motif"] = v["motif"]
            if v.get("transferability"):
                item_obj["transferability"] = v["transferability"]
            if v and not v.get("is_graphic_print", True):
                item_obj["pattern_type"] = v.get("pattern_type", "uncertain")
            compact_items.append(item_obj)

        reduce_payload = build_cluster_request_payload(compact_items, model=cfg["model"])
        reduce_timeout = max(120.0, self.timeout)
        resp = self._call_chat_completions(
            reduce_payload,
            cfg=cfg,
            max_retries=1,
            timeout_override=reduce_timeout,
        )
        content = _extract_assistant_content(resp)
        finish_reason = _get_finish_reason(resp)
        try:
            parsed = parse_llm_json_any(content)
        except AIResponseParseError as exc:
            if finish_reason == "length":
                raise AIResponseParseError(
                    "AI 聚类输出超过长度上限被截断，建议将「AI 范围」调整为 Top 50 后重试。"
                ) from exc
            raise

        raw_clusters = _extract_raw_clusters_list(parsed)
        if not raw_clusters:
            print(
                f"[HotPrint AI] Reduce returned 0 raw clusters (finish_reason={finish_reason}, preview={content[:160]!r})",
                file=sys.stderr,
                flush=True,
            )

        # Server-side sanitization:
        # 1. Drop catch-all / misc names
        # 2. Keep only valid candidate ASINs, deduplicated per cluster
        # 3. Allow multi-label (1 ASIN in up to 3 trend clusters)
        # 4. Enforce >= 2 ASINs per trend cluster
        asin_cluster_count: Dict[str, int] = {}
        clean_clusters: List[Dict[str, Any]] = []
        rejected_clusters: List[Dict[str, Any]] = []
        seen_names = set()

        for rc in raw_clusters:
            if not isinstance(rc, dict):
                continue
            name = str(rc.get("name") or rc.get("theme") or rc.get("title") or "").strip()[:64]
            if is_forbidden_cluster_name(name):
                rejected_clusters.append({"name": name, "reason": "forbidden_catch_all"})
                continue
            name_key = name.lower()
            if name_key in seen_names:
                rejected_clusters.append({"name": name, "reason": "duplicate_name"})
                continue

            raw_asins = rc.get("asins")
            if not isinstance(raw_asins, list):
                raw_asins = rc.get("asin_list") if isinstance(rc.get("asin_list"), list) else (
                    rc.get("items") if isinstance(rc.get("items"), list) else []
                )
            cluster_asins: List[str] = []
            seen_in_cluster = set()
            for a in raw_asins:
                au = str(a.get("asin") if isinstance(a, dict) else (a or "")).strip().upper()
                if au in valid_asin_set and au not in seen_in_cluster:
                    if asin_cluster_count.get(au, 0) < 3:
                        seen_in_cluster.add(au)
                        cluster_asins.append(au)

            if len(cluster_asins) < 2:
                rejected_clusters.append({"name": name, "reason": "fewer_than_2_asins"})
                continue

            for au in cluster_asins:
                asin_cluster_count[au] = asin_cluster_count.get(au, 0) + 1
            seen_names.add(name_key)

            style_tags = _normalize_theme_tags(rc.get("style_tags"))
            target_audience = str(rc.get("target_audience") or "").strip()[:80]
            notes = str(rc.get("notes") or "").strip()[:1000]
            variants = normalize_variant_suggestions(rc.get("variant_suggestions"))
            clean_clusters.append(
                {
                    "name": name,
                    "style_tags": style_tags,
                    "target_audience": target_audience,
                    "notes": notes,
                    "variant_suggestions": variants,
                    "asins": cluster_asins,
                }
            )

        return {
            "dry_run": False,
            "map_stats": {
                "candidate_count": len(candidates),
                "cached_hits": len(cached_pairs),
                "image_calls_executed": image_calls_executed,
                "image_calls_failed": len(map_errors),
                "errors": map_errors[:10],
            },
            "clusters": clean_clusters,
            "rejected_clusters": rejected_clusters,
            "model": cfg["model"],
        }

    # ------------------------------------------------------------------
    # Stage 06 On-Demand POD Remix Variant Suggestions
    # ------------------------------------------------------------------
    def generate_direction_variants(
        self,
        direction_info: Dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Generate 3~5 actionable POD remix variant suggestions for a single direction."""
        cfg_status = self.get_config_status()
        existing = normalize_variant_suggestions(direction_info.get("variant_suggestions"))
        linked_count = len(direction_info.get("linked_products") or [])
        if dry_run:
            return {
                "dry_run": True,
                "direction_id": direction_info.get("id"),
                "direction_name": direction_info.get("name") or "",
                "linked_count": linked_count,
                "existing_count": len(existing),
                "api_calls": 1,
                "model": cfg_status["model"],
                "base_url": cfg_status["base_url"],
                "has_api_key": cfg_status["has_api_key"],
            }

        cfg = self._require_ready_config()
        payload = build_variants_request_payload(direction_info, model=cfg["model"])
        resp = self._call_chat_completions(payload, cfg=cfg, max_retries=1)
        content = _extract_assistant_content(resp)
        parsed = parse_llm_json_any(content)
        raw_list: Any = None
        if isinstance(parsed, list):
            raw_list = parsed
        elif isinstance(parsed, dict):
            for key in ("variant_suggestions", "variants", "suggestions", "ideas", "items"):
                if parsed.get(key) is not None:
                    raw_list = parsed.get(key)
                    break
        variants = normalize_variant_suggestions(raw_list, max_items=5)
        if not variants:
            raise AIResponseParseError("模型未返回有效的变体建议列表，请重试。")
        return {
            "dry_run": False,
            "direction_id": direction_info.get("id"),
            "variant_suggestions": variants,
            "model": cfg["model"],
        }
