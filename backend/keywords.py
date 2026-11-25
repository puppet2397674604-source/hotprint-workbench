"""Domain-specific N-gram print/theme keyword extraction, noise cleaning, and sub-word suppression.

Extracts clean 1-gram, 2-gram, and 3-gram print/theme phrases from Amazon apparel titles
and descriptions using Python stdlib only:
- Splits titles by punctuation clauses so phrases never stitch across commas/pipes/slashes/dashes.
- Filters Amazon garment/silhouette/care/SEO boilerplate stopwords while preserving idiomatic
  multi-word print themes (e.g. `Thanksgiving Day`, `Super Hero`, `Ugly Christmas Sweater`, `Cat Mom`).
- Deduplicates per product (Document Frequency) so SEO keyword stuffing cannot inflate weights.
- Runs 3-gram -> 2-gram -> 1-gram sub-word suppression (ratio >= 0.6 hides sub-word; else deducts).
- Preserves four-theme color classification (`holiday` / `motif` / `style` / `vibe`) for phrases.
"""

from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# Punctuation clause splitter: never form phrases across these boundaries
CLAUSE_SPLIT_RE = re.compile(r"(?:[,|/;()\[\]{}–—:\n!?•·]+|\s+-\s+|\s+&\s+)")
TOKEN_RE = re.compile(r"[a-zA-Z0-9]+(?:['’-][a-zA-Z0-9]+)*")

# Generic English function words & ecommerce filler (never appear as standalone 1-grams)
ENGLISH_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "our", "are", "was",
    "were", "will", "can", "could", "would", "should", "may", "might", "must", "not", "but",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "only",
    "own", "same", "than", "too", "very", "just", "into", "onto", "over", "under", "about",
    "after", "before", "between", "through", "during", "without", "within", "along", "across",
    "around", "above", "below", "also", "even", "still", "well", "back", "there", "their",
    "they", "them", "these", "those", "its", "it's", "who", "what", "when", "where", "which",
    "while", "how", "why", "one", "two", "three", "four", "five", "new", "day", "days", "time",
    "year", "years", "way", "ways", "make", "makes", "made", "making", "get", "gets", "give",
    "gives", "giving", "add", "adds", "adding", "added", "keep", "keeps", "keeping", "stay",
    "stays", "bring", "brings", "show", "shows", "showing", "ensure", "ensures", "provide",
    "provides", "offer", "offers", "offering", "featuring", "features", "feature", "featured",
    "including", "include", "includes", "available", "please", "note", "check", "refer",
    "buying", "ordering", "order", "purchase", "item", "items", "product", "products", "piece",
    "pieces", "great", "perfect", "best", "good", "nice", "awesome", "amazing", "wonderful",
    "ideal", "choice", "favorite", "favourite", "essential", "ready", "whether", "every",
    "everyone", "anyone", "someone", "something", "anything", "everything", "nothing",
    "because", "since", "until", "unless", "though", "although", "however", "therefore",
    "plus", "extra", "super", "ultra", "truly", "really", "always", "never", "often", "here",
    "out", "off", "down", "away", "near", "far", "full", "half", "part", "set", "use",
    "used", "using", "let", "lets", "help", "helps", "like", "love", "loved", "loving", "enjoy",
    "feel", "feels", "feeling", "look", "looks", "looking", "create", "creates",
    "creating", "stand", "stands", "built", "crafted", "designed", "design", "designs",
    "comes", "come", "going", "goes", "take", "takes", "need", "needs", "want", "wants",
    "find", "found", "different", "various", "many", "much", "little", "big", "small",
    "medium", "large", "xl", "xxl", "xxxl", "have", "has", "had", "having", "been", "being",
    "does", "did", "doing", "done", "etc", "suitable", "perfectly", "carefully", "avoid",
    "simple", "simply", "life", "living", "photo", "photos", "picture", "pictures", "monitor",
    "screen", "actual", "slight", "slightly", "manual", "allow", "error", "due", "difference",
    "differences", "attention", "service", "contact", "customer", "satisfaction", "question",
    "questions", "reply", "hours", "return", "exchange", "refund", "shipping", "fast",
    "better", "again", "further", "then", "once", "now", "ever", "usually", "sometimes",
    "almost", "enough", "quite", "rather", "pretty", "already", "yet", "soon", "today",
    "night", "nights", "week", "weeks", "month", "entire", "whole", "complete", "total",
    "single", "double", "first", "second", "third", "last", "next", "final", "main", "real",
    "right", "left", "center", "middle", "upper", "lower", "inner", "outer", "outside",
    "open", "close", "closed", "wide", "narrow", "deep", "hard", "fresh", "turn", "turns",
    "turning", "head", "heads", "compliment", "compliments", "crowd", "crowds", "everybody",
    "anywhere", "everywhere", "anytime", "whenever", "wherever", "whatever", "celebration",
    "celebrations", "celebrate", "celebrating", "festivities", "festivity", "enjoying",
    "wearing", "matching", "pairing", "relaxing", "hanging", "working", "playing",
    "coming", "taking", "putting", "helping", "standing", "keeping", "bringing",
    "showing", "giving", "getting", "making", "adding", "running",
    "walking", "sitting", "shopping", "vacation", "vacations", "holidaying",
    "weekend", "weekends", "dating", "date", "club", "beach", "picnic", "camping", "hiking",
    "workout", "fitness", "yoga", "gym", "silhouette", "flattering", "drape", "drapes",
    "breathability", "comfortably", "easily", "quickly", "gently", "strongly", "especially",
    "particularly", "highly", "fully", "completely", "totally", "sure", "certain",
    "able", "worth", "pair", "style", "off", "per", "via", "non", "pre",
    "2024", "2025", "2026", "2027",
}

# Apparel silhouette, fabric, sizing, washing care & Amazon SEO boilerplate
APPAREL_NOISE_WORDS: Set[str] = {
    "women", "womens", "woman", "woman's", "men", "mens", "man", "man's", "unisex", "ladies",
    "lady", "female", "male", "adult", "adults", "girl", "girls", "boy", "boys", "teen",
    "teens", "junior", "juniors", "people", "wearer", "youth", "kids", "kid",
    "sweatshirt", "sweatshirts", "shirt", "shirts", "tshirt", "tshirts", "t-shirt", "t-shirts",
    "tee", "tees", "pullover", "pullovers", "top", "tops", "sweater", "sweaters", "hoodie",
    "hoodies", "tank", "tanks", "raglan", "henley", "polo", "jersey", "triblend", "ringspun",
    "preshrunk", "tagless", "combed", "heather", "heathered", "cropped", "crop", "scoop",
    "racerback", "blouse", "blouses", "tunic", "tunics", "jumper", "jumpers", "clothes", "clothing",
    "apparel", "garment", "garments", "outfit", "outfits", "wear", "wearable",
    "wardrobe", "closet", "attire", "dress", "dressed", "dressing", "dressy",
    "sleeve", "sleeves", "long", "short", "neck", "neckline", "crewneck", "crew", "mock",
    "mockneck", "mocknecks", "round", "vneck", "v-neck", "collar", "cuff", "cuffs", "hem", "ribbed",
    "pocket", "pockets", "zip", "zipper", "quarter", "button", "buttons", "drop", "shoulder",
    "shoulders", "chest", "torso", "waist", "body", "cut", "shape", "length", "hip",
    "oversized", "loose", "relaxed", "regular", "slim", "baggy", "boxy", "fitted", "fit",
    "fits", "fitting", "size", "sizes", "sized", "sizing", "chart", "measurement",
    "measurements", "true", "recommend", "recommended",
    "fabric", "fabrics", "material", "materials", "cotton", "polyester", "spandex", "rayon",
    "nylon", "acrylic", "fleece", "terry", "french", "blend", "blended", "knit", "knitted",
    "woven", "lining", "lined", "soft", "softer", "softness", "comfortable", "comfort",
    "comfy", "cozy", "breathable", "lightweight", "heavyweight", "midweight", "thick", "thin",
    "warm", "warmer", "warmth", "skin", "friendly", "touch", "smooth", "gentle", "quality",
    "durable", "durability", "high", "stretch", "stretchy", "elastic", "elasticity", "shrink",
    "shrinking", "fade", "fading", "pill", "pilling", "wrinkle", "buttery", "modal",
    "wash", "washed", "washing", "washable", "machine", "hand", "cold", "water",
    "bleach", "dry", "drying", "hang", "line", "flat", "tumble", "low", "heat", "iron",
    "ironing", "care", "cycle", "detergent", "clean", "cleaning", "separate", "separately",
    "inside", "similar",
    "jeans", "leggings", "pants", "trousers", "shorts", "skirt", "skirts", "boots", "sneakers",
    "shoes", "jacket", "jackets", "coat", "coats", "cardigan", "vest", "scarf", "hat", "layer",
    "layers", "layering", "layered", "pairs", "matched", "easy", "effortless", "effortlessly",
    "versatile", "styles", "styled", "styling", "stylish", "trendy", "fashion", "fashionable",
    "classic", "chic", "modern", "casual", "daily", "everyday", "lounging", "lounge", "home",
    "work", "office", "school", "street", "travel", "outdoor", "indoor", "activities", "activity",
    "event", "events", "occasion", "occasions", "gathering", "gatherings", "outing", "outings",
    "weather", "chilly", "cool", "cooler", "crisp", "months", "morning",
    "evening", "season", "seasons", "seasonal", "autumn", "spring",
    "color", "colors", "colour", "colours", "black", "white", "green", "red", "blue", "gray",
    "grey", "pink", "brown", "beige", "navy", "purple", "orange", "yellow", "khaki", "cream",
    "dark", "light", "solid", "plain", "basic", "print", "printed", "prints", "printing",
    "pattern", "patterns", "patterned", "novelty", "graphic", "graphics", "funny",
    "detail", "details", "detailed", "front", "back", "side", "allover", "vibrant", "bold",
    "eye", "catching", "unique", "special", "fun", "statement", "vibe", "vibes", "spirit",
    "charm", "flair", "element", "elements", "theme", "themed", "motif", "motifs", "artwork",
    "art", "text", "word", "words", "saying", "sayings", "quote", "quotes", "message",
    "gift", "gifts", "gifting", "present", "presents", "birthday", "thoughtful", "yourself",
    "ones", "wife", "husband", "mom", "mother", "dad", "father", "sister", "brother",
    "friend", "friends", "family", "families", "daughter", "son", "girlfriend", "boyfriend",
    "teacher", "coworker", "amazon", "merch", "demand", "custom", "image", "modify", "pack",
    "count", "inch", "inches", "cm", "officially", "licensed", "premium", "standard",
    "tight", "see", "idea", "ideas", "humor", "humorous", "sarcastic", "hilarious",
}

# Idiomatic multi-word print/theme phrases that contain a stopword modifier (e.g. day/super/mom/sweater)
# Matched strictly WITHIN a single punctuation clause (never across commas/pipes/slashes)
CURATED_COMPOUND_PHRASES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bugly\s+(?:christmas|xmas)\s+sweaters?\b", re.I), "ugly christmas sweater"),
    (re.compile(r"\bthanksgiving\s+days?\b", re.I), "thanksgiving day"),
    (re.compile(r"\bsuper[\s-]?hero(?:es)?\b", re.I), "super hero"),
    (re.compile(r"\bcat\s+moms?\b", re.I), "cat mom"),
    (re.compile(r"\bdog\s+moms?\b", re.I), "dog mom"),
    (re.compile(r"\bdad\s+jokes?\b", re.I), "dad joke"),
    (re.compile(r"\bmama\s+bears?\b", re.I), "mama bear"),
    (re.compile(r"\bcare\s+bears?\b", re.I), "care bear"),
    (re.compile(r"\bst\.?\s*patrick'?s?\s+day\b", re.I), "st patrick day"),
    (re.compile(r"\bvalentine'?s?\s+day\b", re.I), "valentine day"),
    (re.compile(r"\bmother'?s?\s+day\b", re.I), "mother day"),
    (re.compile(r"\bfather'?s?\s+day\b", re.I), "father day"),
    (re.compile(r"\bnew\s+year'?s?\b", re.I), "new year"),
    (re.compile(r"\btrick\s+or\s+treat\b", re.I), "trick or treat"),
    (re.compile(r"\bthankful\s+grateful\s+blessed\b", re.I), "thankful grateful blessed"),
    (re.compile(r"\bmerry\s+and\s+bright\b", re.I), "merry and bright"),
    (re.compile(r"\bgood\s+vibes\b", re.I), "good vibes"),
    (re.compile(r"\btie[\s-]?dye\b", re.I), "tie dye"),
    (re.compile(r"\bamerican\s+flag\b", re.I), "american flag"),
    (re.compile(r"\bhairy\s+chest\b", re.I), "hairy chest"),
    (re.compile(r"\bcoffee\s+lovers?\b", re.I), "coffee lover"),
    (re.compile(r"\bbook\s+lovers?\b", re.I), "book lover"),
    (re.compile(r"\bmental\s+health\b", re.I), "mental health"),
    (re.compile(r"\bpumpkin\s+spice\b", re.I), "pumpkin spice"),
    (re.compile(r"\bleopard\s+print\b", re.I), "leopard print"),
    (re.compile(r"\bfloral\s+print\b", re.I), "floral print"),
]

# Canonical lemmatization / synonym mapping for print themes & motifs
CANONICAL_MAP: Dict[str, str] = {
    "xmas": "christmas",
    "christmassy": "christmas",
    "holidays": "holiday",
    "ghosts": "ghost",
    "pumpkins": "pumpkin",
    "turkeys": "turkey",
    "cats": "cat",
    "dogs": "dog",
    "trees": "tree",
    "skeletons": "skeleton",
    "skulls": "skull",
    "witches": "witch",
    "bats": "bat",
    "bows": "bow",
    "hearts": "heart",
    "flowers": "floral",
    "flower": "floral",
    "letters": "letter",
    "lettering": "letter",
    "sequins": "sequin",
    "sequined": "sequin",
    "embroidery": "embroidered",
    "patches": "patch",
    "stripes": "striped",
    "stripe": "striped",
    "parties": "party",
    "snowmen": "snowman",
    "snowflakes": "snowflake",
    "reindeers": "reindeer",
    "elves": "elf",
    "gnomes": "gnome",
    "frogs": "frog",
    "bears": "bear",
    "books": "book",
    "jokes": "joke",
    "memes": "meme",
    "christians": "christian",
    "paws": "paw",
    "stars": "star",
    "lights": "lights",
    "gingerbreads": "gingerbread",
    "nutcrackers": "nutcracker",
    "ruffles": "ruffle",
    "lanterns": "lantern",
    "goblins": "goblin",
    "zombies": "zombie",
    "vampires": "vampire",
    "grinches": "grinch",
    "cowboys": "cowboy",
    "dinosaurs": "dinosaur",
    "mushrooms": "mushroom",
    "costumes": "costume",
    "fans": "fan",
    "sports": "sport",
    "games": "game",
    "faces": "face",
    "heroes": "hero",
    "lovers": "lover",
    "sweaters": "sweater",
    "days": "day",
}

DISPLAY_OVERRIDES: Dict[str, str] = {
    "usa": "USA",
    "diy": "DIY",
    "y2k": "Y2K",
    "nfl": "NFL",
    "christmas": "Christmas",
    "halloween": "Halloween",
    "thanksgiving": "Thanksgiving",
    "santa": "Santa",
    "snoopy": "Snoopy",
    "peanuts": "Peanuts",
    "grinch": "Grinch",
    "jesus": "Jesus",
    "christian": "Christian",
    "st": "St.",
    "and": "and",
    "or": "or",
}

# Semantic groups for vivid 4-dimension color coding in the word cloud
HOLIDAY_WORDS: Set[str] = {
    "christmas", "halloween", "thanksgiving", "holiday", "festive", "santa", "merry",
    "winter", "fall", "snow", "snowman", "snowflake", "reindeer", "elf", "gingerbread",
    "nutcracker", "jolly", "grinch", "claus", "sleigh", "north", "pole", "eve", "december",
    "october", "november", "harvest", "gobble", "thankful", "grateful", "blessed",
    "valentine", "patrick", "easter", "spooky",
}
MOTIF_WORDS: Set[str] = {
    "ghost", "pumpkin", "turkey", "cat", "dog", "tree", "skeleton", "skull", "witch",
    "leopard", "floral", "bow", "heart", "star", "cross", "lantern", "bat", "spider",
    "gnome", "bear", "frog", "book", "coffee", "wine", "beer", "flag", "eagle", "usa",
    "football", "game", "paw", "dino", "dinosaur", "mushroom", "cowboy", "western",
    "snoopy", "peanuts", "charlie", "woodstock", "frankenstein", "hairy", "chest", "face",
    "lights", "plaid", "checkered", "camo", "animal", "cheetah", "hero", "superman",
    "batman", "dragonfly", "crayon", "zombie", "mummy", "vampire", "sunflower", "butterfly",
}
STYLE_WORDS: Set[str] = {
    "ugly", "vintage", "retro", "embroidered", "sequin", "chenille", "patch",
    "striped", "letter", "personalized", "tie", "dye", "distressed", "aesthetic", "boho",
    "preppy", "coquette", "gothic", "punk", "streetwear", "hippie", "country", "neon",
    "glitter", "sparkle", "shiny", "ruffle", "balloon", "contrast", "comic", "y2k",
}

SUBWORD_SUPPRESS_RATIO = 0.60


def _classify_theme(phrase: str) -> str:
    """Classify a 1/2/3-gram phrase into holiday / motif / style / vibe."""
    words = phrase.split()
    # Priority: holiday > motif > style > vibe (checks any token in phrase)
    if any(w in HOLIDAY_WORDS for w in words):
        return "holiday"
    if any(w in MOTIF_WORDS for w in words):
        return "motif"
    if any(w in STYLE_WORDS for w in words):
        return "style"
    return "vibe"


def _format_display(phrase: str) -> str:
    parts = phrase.split()
    out: List[str] = []
    for idx, w in enumerate(parts):
        if w in DISPLAY_OVERRIDES:
            word = DISPLAY_OVERRIDES[w]
            if idx == 0 and word in ("and", "or"):
                word = word.capitalize()
            out.append(word)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return round(s[mid], 1)
    return round((s[mid - 1] + s[mid]) / 2.0, 1)


def _extract_clause_ngrams(text: str, noise: Set[str]) -> Set[str]:
    """Extract unique 1-gram, 2-gram, and 3-gram candidates from a text without crossing punctuation."""
    found: Set[str] = set()
    if not text:
        return found

    clauses = [c.strip() for c in CLAUSE_SPLIT_RE.split(text) if c and c.strip()]
    for clause in clauses:
        # 1. Curated compound phrases (strictly within the same punctuation clause)
        for pattern, canonical_phrase in CURATED_COMPOUND_PHRASES:
            if pattern.search(clause):
                found.add(canonical_phrase)

        # 2. Tokenize and break into contiguous non-stopword runs
        run: List[str] = []

        def flush_run(seq: List[str]) -> None:
            n = len(seq)
            if n == 0:
                return
            for i in range(n):
                found.add(seq[i])
                if i + 1 < n and seq[i] != seq[i + 1]:
                    found.add(f"{seq[i]} {seq[i + 1]}")
                if i + 2 < n and len({seq[i], seq[i + 1], seq[i + 2]}) == 3:
                    found.add(f"{seq[i]} {seq[i + 1]} {seq[i + 2]}")

        for raw_tok in TOKEN_RE.findall(clause):
            low = raw_tok.lower().replace("’", "'")
            # Treat explicit hyphenated garment compounds (t-shirt, v-neck, long-sleeve) as stopword boundary
            if low in noise:
                flush_run(run)
                run = []
                continue
            # Strip trailing possessive 's
            if low.endswith("'s"):
                low = low[:-2]
            # Split remaining hyphenated non-stopword compounds into sub-tokens
            sub_parts = [p for p in re.split(r"[-']", low) if p]
            for part in sub_parts:
                if not part.isalpha() or len(part) < 3:
                    flush_run(run)
                    run = []
                    continue
                canon = CANONICAL_MAP.get(part, part)
                if canon in noise or part in noise:
                    flush_run(run)
                    run = []
                else:
                    run.append(canon)
        flush_run(run)

    return found


def _sub_ngrams(phrase: str) -> List[str]:
    """Return all proper contiguous sub-ngrams (2-grams and 1-grams) of a multi-word phrase."""
    parts = phrase.split()
    n = len(parts)
    if n <= 1:
        return []
    subs: List[str] = []
    if n == 3:
        subs.append(f"{parts[0]} {parts[1]}")
        subs.append(f"{parts[1]} {parts[2]}")
    for w in parts:
        subs.append(w)
    return subs


def extract_keyword_cloud(products: List[Dict[str, Any]], max_words: int = 44) -> Dict[str, Any]:
    """Extract cleaned 1/2/3-gram print/theme keywords with Document Frequency & sub-word suppression.

    Backward-compatible output payload for `/api/overview` and `frontend/js/charts.js`.
    """
    total_products = len(products)
    base_noise = ENGLISH_STOPWORDS | APPAREL_NOISE_WORDS
    if total_products == 0:
        return {
            "total_products": 0,
            "products_with_description": 0,
            "cleaned_stopword_count": len(base_noise),
            "keywords": [],
            "top_keywords": [],
        }

    # Dynamically collect brand tokens (exclude brand spam without blocking real motif/holiday words)
    curated_keep = HOLIDAY_WORDS | MOTIF_WORDS | STYLE_WORDS
    brand_tokens: Set[str] = set()
    for p in products:
        brand = (p.get("brand") or "").lower()
        if len(brand.split()) <= 2:
            for w in re.findall(r"[a-z]{3,}", brand):
                canon_w = CANONICAL_MAP.get(w, w)
                if canon_w not in curated_keep:
                    brand_tokens.add(w)
                    brand_tokens.add(canon_w)

    noise = base_noise | brand_tokens

    doc_products: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    title_hits: Dict[str, int] = defaultdict(int)
    desc_hits: Dict[str, int] = defaultdict(int)
    products_with_desc = 0

    for p in products:
        title_text = p.get("title") or ""
        desc_text = " ".join(filter(None, [p.get("subtitle") or "", p.get("description") or ""]))
        if desc_text.strip():
            products_with_desc += 1

        # Per-product deduplicated sets (Document Frequency: repeated stuffing in 1 title counts once)
        t_grams = _extract_clause_ngrams(title_text, noise)
        d_grams = _extract_clause_ngrams(desc_text, noise)
        all_grams = t_grams | d_grams

        for gram in all_grams:
            doc_products[gram].append(p)
            if gram in t_grams:
                title_hits[gram] += 1
            if gram in d_grams:
                desc_hits[gram] += 1

    # Thresholds: phrases require >= 2 docs when pool has >= 2 items; 1-grams >= 2 when pool >= 10
    min_phrase_docs = 2 if total_products >= 2 else 1
    min_unigram_docs = 2 if total_products >= 10 else 1

    raw_counts: Dict[str, int] = {g: len(plist) for g, plist in doc_products.items()}
    doc_ids: Dict[str, Set[int]] = {
        g: {id(p) for p in plist} for g, plist in doc_products.items()
    }
    claimed_ids: Dict[str, Set[int]] = defaultdict(set)
    suppressed: Set[str] = set()

    def remaining_count(gram: str) -> int:
        return len(doc_ids[gram] - claimed_ids[gram])

    def is_eligible_gram(gram: str, cnt: int) -> bool:
        parts = gram.split()
        n = len(parts)
        if n == 1:
            if gram in noise:
                return False
            if cnt < min_unigram_docs:
                return False
            if total_products >= 20 and title_hits[gram] == 0 and gram not in curated_keep:
                return False
            if total_products >= 20 and title_hits[gram] == 0 and raw_counts[gram] < 5:
                return False
            return True
        # Multi-word phrase (2-gram / 3-gram)
        if cnt < min_phrase_docs:
            return False
        # Require title appearance on medium/large datasets so boilerplate description phrases don't dominate
        if total_products >= 15 and title_hits[gram] == 0:
            return False
        return True

    # Hierarchical Sub-word Suppression: 3-gram -> 2-gram -> 1-gram (set-difference prevents double deduction)
    for gram_len in (3, 2):
        level_grams = [
            g for g in doc_products
            if len(g.split()) == gram_len and g not in suppressed
        ]
        # Process highest-frequency / title-prominent phrases first
        level_grams.sort(key=lambda g: (-remaining_count(g), -title_hits[g], g))
        for phrase in level_grams:
            rem_ids = doc_ids[phrase] - claimed_ids[phrase]
            c_p = len(rem_ids)
            if not is_eligible_gram(phrase, c_p):
                continue
            for sub in _sub_ngrams(phrase):
                sub_ids = doc_ids.get(sub)
                if not sub_ids:
                    continue
                overlap = rem_ids & sub_ids
                if not overlap:
                    continue
                if (len(overlap) / len(sub_ids)) >= SUBWORD_SUPPRESS_RATIO:
                    suppressed.add(sub)
                else:
                    claimed_ids[sub].update(overlap)

    # Assemble final scored list
    scored: List[Dict[str, Any]] = []
    for gram, matched in doc_products.items():
        if gram in suppressed:
            continue
        rem_cnt = remaining_count(gram)
        if not is_eligible_gram(gram, rem_cnt):
            continue

        parts = gram.split()
        ngram_len = len(parts)
        t_cnt = title_hits[gram]
        d_cnt = desc_hits[gram]
        raw_cnt = raw_counts[gram]
        sales = [
            float(p["listing_sales_30d"])
            for p in matched
            if p.get("listing_sales_30d") is not None
        ]
        # Give high-signal multi-word phrases a mild prominence lift over fragmented 1-grams
        phrase_boost = 1.25 if ngram_len == 3 else (1.12 if ngram_len == 2 else 1.0)
        prominence = (rem_cnt + min(rem_cnt, t_cnt) * 0.85) * phrase_boost

        scored.append(
            {
                "token": gram,
                "word": _format_display(gram),
                "query": gram,
                "ngram": ngram_len,
                "doc_count": rem_cnt,
                "raw_doc_count": raw_cnt,
                "title_count": t_cnt,
                "desc_count": d_cnt,
                "share_pct": round(raw_cnt / total_products * 100.0, 1),
                "median_sales_30d": _median(sales),
                "theme": _classify_theme(gram),
                "_prominence": prominence,
            }
        )

    scored.sort(key=lambda x: (-x["_prominence"], -x["doc_count"], -x["ngram"], x["token"]))
    selected = scored[:max_words]

    if selected:
        vals = [math.log1p(x["_prominence"]) for x in selected]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        for item, v in zip(selected, vals):
            item["weight"] = round((v - lo) / span, 3)
            item.pop("_prominence", None)
    else:
        for item in selected:
            item.pop("_prominence", None)

    return {
        "total_products": total_products,
        "products_with_description": products_with_desc,
        "cleaned_stopword_count": len(noise),
        "keywords": selected,
        "top_keywords": selected[:10],
    }
