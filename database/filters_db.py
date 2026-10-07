import asyncio
import logging
import re
import time
import unicodedata
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError
from config import COLLECTION_NAME
from database.client import db

logger = logging.getLogger(__name__)

files = db[COLLECTION_NAME]

_RESULT_FIELDS = {
    "file_name": 1, "caption": 1, "file_size": 1, "file_id": 1, "file_unique_id": 1,
}


async def ensure_indexes():
    """Call once at startup. Safe to call repeatedly — Mongo no-ops if present."""
    # `words` backs the primary search path (see search_files below) — a
    # plain multikey index Mongo builds automatically for an array field,
    # giving indexed, exact, stopword-free word lookups.
    await files.create_index([("words", ASCENDING)], name="words_idx")
    await files.create_index([("tkeys", ASCENDING)], name="tkeys_idx")
    await files.create_index([("file_unique_id", ASCENDING)], unique=True, name="uniq_file_idx")
    await files.create_index([("channel_id", ASCENDING)], name="channel_idx")
    logger.info("Database indexes ready.")


def display_name(doc: dict) -> str:
    """Captions carry the readable title/quality/language info admins write
    by hand; prefer that everywhere a file is shown, falling back to the
    raw filename only when there's no caption."""
    return (doc.get("caption") or "").strip() or doc.get("file_name", "Unnamed file")


async def export_all_captions(path: str) -> int:
    """Write every indexed file's display caption to a local text file, one
    per line — lets an admin see exactly what's really in the database
    (real formatting, real spelling) before tuning search behaviour.
    Streams via cursor so it stays memory-safe no matter the collection
    size. Returns the number of lines written."""
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        cursor = files.find({}, {"file_name": 1, "caption": 1})
        async for doc in cursor:
            f.write(display_name(doc) + "\n")
            count += 1
    return count


# ══════════════════════════════════════════════════════════════════════════════
# WORD TOKENISING — shared by indexing (save_file) and searching (search_files)
# so the two sides always agree on what counts as "the same word". Splitting
# on any non-alphanumeric character makes it completely punctuation-agnostic
# (dots, underscores, brackets, hyphens all count as a separator) without
# ever altering the letters/numbers themselves — nothing is stemmed,
# corrected, or dropped the way MongoDB's own $text/English-stopword system
# would (that system was quietly discarding words like "and"/"from"
# entirely, which is what let "Vishwanath and Son" degrade into a
# bare "son" search).
# ══════════════════════════════════════════════════════════════════════════════

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list:
    return _WORD_RE.findall(text.lower())


# ══════════════════════════════════════════════════════════════════════════════
# INDEXING (auto + manual share this single entrypoint)
# ══════════════════════════════════════════════════════════════════════════════

async def save_file(media, channel_id: int | None = None) -> str:
    """
    Save one media item. `media` is a pyrogram Document/Video object with
    `.caption` and `.file_type` attached by the caller.

    Returns one of: 'saved', 'updated', 'duplicate', 'skipped', 'error'
    """
    file_name = getattr(media, "file_name", None)
    if not file_name:
        # No filename means nothing useful to search on — skip it.
        return "skipped"

    file_unique_id = getattr(media, "file_unique_id", None)
    if not file_unique_id:
        return "skipped"

    caption = getattr(media, "caption", "") or ""
    words = sorted(set(_tokenize(f"{file_name} {caption}")))
    tkeys = doc_title_keys(file_name, caption)

    doc = {
        "file_id": media.file_id,
        "file_unique_id": file_unique_id,
        "file_name": file_name,
        "file_size": getattr(media, "file_size", 0) or 0,
        "caption": caption,
        "words": words,
        "tkeys": tkeys,
        "tkv": KEY_VERSION,
        "file_type": getattr(media, "file_type", "document"),
        "mime_type": getattr(media, "mime_type", "") or "",
        "channel_id": channel_id,
        "indexed_at": time.time(),
    }

    try:
        await files.insert_one(doc)
        return "saved"
    except DuplicateKeyError:
        # Same file re-posted or re-indexed. If the caption changed, keep it
        # fresh — captions are edited more often than files are re-uploaded.
        existing = await files.find_one({"file_unique_id": file_unique_id}, {"caption": 1})
        if existing is not None and existing.get("caption", "") != caption:
            await files.update_one(
                {"file_unique_id": file_unique_id},
                {"$set": {"caption": caption, "file_name": file_name, "words": words, "tkeys": tkeys, "tkv": KEY_VERSION}},
            )
            return "updated"
        return "duplicate"
    except Exception:
        logger.exception("save_file failed for %s", file_name)
        return "error"


async def total_files() -> int:
    return await files.estimated_document_count()


async def count_by_channel(channel_id: int) -> int:
    return await files.count_documents({"channel_id": channel_id})


async def backfill_word_index(batch_size: int = 500) -> int:
    """
    Migration for files indexed before the `words` / `tkeys` fields existed
    (tkeys = the spacing/symbol-proof title keys Stage 1 matches on).
    Safe to run repeatedly (idempotent) and safe to run while the bot is
    live — updates stream in small batches so it never holds a large chunk
    of the collection in memory or blocks the DB for long. The bot also runs
    this once in the background at startup.
    Returns the number of documents updated.
    """
    from pymongo import UpdateOne

    updated = 0
    batch = []
    cursor = files.find(
        {"$or": [{"words": {"$exists": False}}, {"tkeys": {"$exists": False}},
              {"tkv": {"$ne": KEY_VERSION}}]},
        {"file_name": 1, "caption": 1},
    )
    async for doc in cursor:
        file_name = doc.get("file_name", "") or ""
        caption = doc.get("caption", "") or ""
        words = sorted(set(_tokenize(f"{file_name} {caption}")))
        tkeys = doc_title_keys(file_name, caption)
        batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"words": words, "tkeys": tkeys, "tkv": KEY_VERSION}}))
        if len(batch) >= batch_size:
            await files.bulk_write(batch, ordered=False)
            updated += len(batch)
            batch = []
            await asyncio.sleep(0.05)  # let live searches breathe
    if batch:
        await files.bulk_write(batch, ordered=False)
        updated += len(batch)
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# RESULT FILTERING — season / language / year / quality / episode
# Extracted straight from file_name + caption text. Used two ways:
#   1. Post-search, to power the Season/Language/Year/Quality/Episode
#      filter buttons on a result page.
#   2. Up front, by parse_query() below, to pull the same kind of tag out
#      of the user's own typed query so it narrows Stage 1 search results
#      instead of being searched as if it were part of the title.
# ══════════════════════════════════════════════════════════════════════════════

_SE_PATTERN = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE)
_SEASON_PATTERN = re.compile(r"\bS(\d{1,2})\b|\bSeason\s*(\d{1,2})\b", re.IGNORECASE)
_EPISODE_PATTERN = re.compile(r"\bE(?:p(?:isode)?)?\s*(\d{1,3})\b", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")

# canonical language -> every spelling/abbreviation seen in real captions
# that should count as that language. Keep this evidence-based: only add an
# abbreviation here once you've actually seen it used, since a short
# abbreviation can coincide with an unrelated real word (e.g. "pun" is also
# an English word) — the full names carry no such risk.
_LANGUAGE_ALIASES = {
    "hindi": ["hindi", "hin"],
    "english": ["english", "eng"],
    "tamil": ["tamil", "tam", "taml"],
    "telugu": ["telugu", "tel"],
    "kannada": ["kannada"],
    "malayalam": ["malayalam"],
    "bengali": ["bengali"],
    "punjabi": ["punjabi"],
    "marathi": ["marathi"],
    "gujarati": ["gujarati"],
    "urdu": ["urdu"],
    "korean": ["korean"],
    "japanese": ["japanese"],
    "chinese": ["chinese"],
    "spanish": ["spanish"],
    "french": ["french"],
    "german": ["german"],
    "dual audio": ["dual audio", "dual"],
    "multi audio": ["multi audio", "multi"],
}
_LANG_ALIAS_PATTERNS = {
    canonical: re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE)
    for canonical, aliases in _LANGUAGE_ALIASES.items()
}

# (regex, code, label) — first match wins, ordered best quality first
_QUALITY_RULES = [
    (re.compile(r"\b(2160p|4k|uhd)\b", re.IGNORECASE), "2160p", "4K / 2160p"),
    (re.compile(r"\b1080p\b", re.IGNORECASE), "1080p", "1080p"),
    (re.compile(r"\b720p\b", re.IGNORECASE), "720p", "720p"),
    (re.compile(r"\b480p\b", re.IGNORECASE), "480p", "480p"),
    (re.compile(r"\b360p\b", re.IGNORECASE), "360p", "360p"),
    (re.compile(r"\bblu-?ray|bdrip\b", re.IGNORECASE), "bluray", "BluRay"),
    (re.compile(r"\bweb-?dl\b", re.IGNORECASE), "webdl", "WEB-DL"),
    (re.compile(r"\bwebrip\b", re.IGNORECASE), "webrip", "WEBRip"),
    (re.compile(r"\bhdrip\b", re.IGNORECASE), "hdrip", "HDRip"),
    (re.compile(r"\bhdtv\b", re.IGNORECASE), "hdtv", "HDTV"),
    (re.compile(r"\bdvdrip\b", re.IGNORECASE), "dvdrip", "DVDRip"),
    (re.compile(r"\b(cam|hdts|ts)\b", re.IGNORECASE), "cam", "CAM/TS"),
]
_QUALITY_RANK = {code: len(_QUALITY_RULES) - i for i, (_pat, code, _label) in enumerate(_QUALITY_RULES)}


def _doc_text(doc: dict) -> str:
    return f"{doc.get('file_name', '')} {doc.get('caption', '') or ''}"


def _season_of(text: str) -> int:
    m = _SE_PATTERN.search(text)
    if m:
        return int(m.group(1))
    m = _SEASON_PATTERN.search(text)
    if m:
        return int(m.group(1) or m.group(2))
    return 0


def _episode_of(text: str) -> int:
    m = _SE_PATTERN.search(text)
    if m:
        return int(m.group(2))
    m = _EPISODE_PATTERN.search(text)
    if m:
        return int(m.group(1))
    return 0


def _year_of(text: str) -> str:
    m = _YEAR_PATTERN.search(text)
    return m.group(0) if m else ""


def _languages_of(text: str) -> list:
    return [canonical for canonical, pat in _LANG_ALIAS_PATTERNS.items() if pat.search(text)]


def _language_matches(text: str, canonical: str) -> bool:
    pat = _LANG_ALIAS_PATTERNS.get(canonical)
    return bool(pat and pat.search(text))


def _quality_of(text: str) -> tuple:
    for pat, code, label in _QUALITY_RULES:
        if pat.search(text):
            return code, label
    return "", ""


def _quality_rank(text: str) -> int:
    code, _label = _quality_of(text)
    return _QUALITY_RANK.get(code, 0)


# ══════════════════════════════════════════════════════════════════════════════
# RESOLUTION / SOURCE TAGS used by SEARCH (480p, 720p, 1080p ... and WEB-DL,
# HDRip, HDTC, HD ...). They are two separate, independent tags, so
# "1080p WEB-DL" means "1080p AND WEB-DL" — a file named "... 1080p WEB-DL"
# matches both. (The single Quality filter button above is unchanged.)
# Every pattern accepts each separator seen in real file names:
# "web dl", "web-dl", "web.dl", "web_dl", "webdl".
# ══════════════════════════════════════════════════════════════════════════════

_SEP = r"[\s\-._]?"

# (code, regex)
_RESOLUTION_RULES = [
    ("2160p", r"2160p|4k|uhd"),
    ("1440p", r"1440p"),
    ("1080p", r"1080p"),
    ("720p", r"720p"),
    ("576p", r"576p"),
    ("480p", r"480p"),
    ("360p", r"360p"),
    ("240p", r"240p"),
]

# (code, regex, word-index token alternatives). Specific tags come first and
# plain "hd" last, so "hd rip" is read as HDRip and never as "hd" + "rip".
_SOURCE_RULES = [
    ("webdl", rf"web{_SEP}dl", [["webdl"], ["web", "dl"]]),
    ("webrip", rf"web{_SEP}rip", [["webrip"], ["web", "rip"]]),
    ("bluray", rf"blu{_SEP}ray|bd{_SEP}rip|br{_SEP}rip",
     [["bluray"], ["blu", "ray"], ["bdrip"], ["bd", "rip"], ["brrip"], ["br", "rip"]]),
    ("hdrip", rf"hd{_SEP}rip", [["hdrip"], ["hd", "rip"]]),
    ("hdtv", r"hdtv", [["hdtv"]]),
    ("hdtc", rf"hd{_SEP}tc", [["hdtc"], ["hd", "tc"]]),
    ("hdcam", rf"hd{_SEP}cam", [["hdcam"], ["hd", "cam"]]),
    ("dvdrip", rf"dvd{_SEP}rip", [["dvdrip"], ["dvd", "rip"]]),
    ("dvdscr", rf"dvd{_SEP}scr", [["dvdscr"], ["dvd", "scr"]]),
    ("predvd", rf"pre{_SEP}dvd", [["predvd"], ["pre", "dvd"]]),
    ("ts", rf"hd{_SEP}ts|ts|telesync", [["hdts"], ["hd", "ts"], ["ts"], ["telesync"]]),
    ("cam", rf"cam(?:{_SEP}rip)?", [["cam"], ["camrip"], ["cam", "rip"]]),
    ("hd", rf"hd(?!{_SEP}(?:rip|tc|cam|ts)\b)", [["hd"]]),
]

_RES_TEXT_PATTERNS = {
    code: re.compile(rf"\b(?:{pat})\b", re.IGNORECASE) for code, pat in _RESOLUTION_RULES
}
_RES_FULL_PATTERNS = {
    code: re.compile(pat, re.IGNORECASE) for code, pat in _RESOLUTION_RULES
}
_SOURCE_TEXT_PATTERNS = {
    code: re.compile(rf"\b(?:{pat})\b", re.IGNORECASE) for code, pat, _t in _SOURCE_RULES
}
_SOURCE_FULL_PATTERNS = {
    code: re.compile(pat, re.IGNORECASE) for code, pat, _t in _SOURCE_RULES
}
_SOURCE_TOKENS = {code: tokens for code, _pat, tokens in _SOURCE_RULES}


def _resolution_code_of(tag: str):
    tag = tag.strip()
    for code, _pat in _RESOLUTION_RULES:
        if _RES_FULL_PATTERNS[code].fullmatch(tag):
            return code
    return None


def _source_code_of(tag: str):
    tag = tag.strip()
    for code, _pat, _tokens in _SOURCE_RULES:
        if _SOURCE_FULL_PATTERNS[code].fullmatch(tag):
            return code
    return None


def _resolution_matches(text: str, codes) -> bool:
    """True if the text carries ANY of the given resolution(s)."""
    codes = [codes] if isinstance(codes, str) else list(codes)
    return any(_RES_TEXT_PATTERNS[c].search(text) for c in codes if c in _RES_TEXT_PATTERNS)


def _source_matches(text: str, codes) -> bool:
    """True if the text carries EVERY given source tag."""
    codes = [codes] if isinstance(codes, str) else list(codes)
    return all(_SOURCE_TEXT_PATTERNS[c].search(text) for c in codes if c in _SOURCE_TEXT_PATTERNS)


# ══════════════════════════════════════════════════════════════════════════════
# CLEAN TITLE — cuts everything from the first season/episode/year/quality/
# language/technical-junk marker onward, since in real captions those tags
# always come after the title, never before or in the middle. This is the
# single canonical way both a query and a database caption get reduced to
# "just the title" before Stage 1 compares them for an exact match.
# ══════════════════════════════════════════════════════════════════════════════

# ── emoji / symbol / empty-bracket removal ──────────────────────────────────
# Used for everything shown to users (result page, trending list) and inside
# clean_title(), so a caption like "🎬 Movie ✦ 2024 ★ [ ]" is understood as
# plain "Movie 2024". Letters, digits, punctuation and every language's
# script are never touched. Removed: emojis (flags, skin tones, joined
# emojis, keycaps), decorative symbols (★ ✦ ➤ ● ◆ ♥ © ® ™ → | ~ • « » ...)
# and empty brackets ([] () {}). The zero-width joiner is only removed when
# it glues emojis together, so Indic scripts that use it stay intact.

def _build_decor_class() -> str:
    keep = set("°´")
    spans = []

    # every "Symbol, other/modifier" code point outside the letter scripts
    # (Greek ... Indic ... Tibetan, U+0370-U+1FFF, are skipped entirely)
    for cp in list(range(0x80, 0x370)) + list(range(0x2000, 0x20000)):
        ch = chr(cp)
        if ch not in keep and unicodedata.category(ch) in ("So", "Sk"):
            spans.append((cp, cp))
    # wholesale emoji / symbol blocks (also covers emojis newer than this
    # Python's Unicode database)
    spans.extend([
        (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
        (0x2190, 0x21FF), (0x27F0, 0x27FF), (0x2900, 0x297F),
        (0xFE0E, 0xFE0F), (0x20E3, 0x20E3), (0xE0020, 0xE007F),
        (0x203C, 0x203C), (0x2049, 0x2049), (0x2139, 0x2139),
        (0x3030, 0x3030), (0x303D, 0x303D),
    ])
    for ch in "|~•‣⁃∙·«»‹›※¦":
        spans.append((ord(ch), ord(ch)))

    spans.sort()
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    def esc(cp):
        return "\\u%04X" % cp if cp <= 0xFFFF else "\\U%08X" % cp

    return "[" + "".join(
        esc(lo) if lo == hi else f"{esc(lo)}-{esc(hi)}" for lo, hi in merged
    ) + "]"


_DECOR_CLASS = _build_decor_class()
_KEYCAP_RE = re.compile(r"[0-9#*]\uFE0F?\u20E3")
_DECOR_RUN_RE = re.compile(_DECOR_CLASS + r"(?:\u200D?" + _DECOR_CLASS + r")*\u200D?")
_EMPTY_BRACKETS_RE = re.compile(r"\[\s*\]|\(\s*\)|\{\s*\}")
_EDGE_JUNK = " \t-–—:,;"


def clean_display_text(text: str) -> str:
    """Remove emojis, decorative symbols and empty brackets; keep the rest of
    the text exactly as it is (only the gaps left behind are tidied)."""
    if not text:
        return ""
    cleaned, n1 = _KEYCAP_RE.subn(" ", text)
    cleaned, n2 = _DECOR_RUN_RE.subn(" ", cleaned)
    n3 = 0
    while True:  # nested leftovers such as "([ ])" collapse step by step
        cleaned, n = _EMPTY_BRACKETS_RE.subn(" ", cleaned)
        if not n:
            break
        n3 += n
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if n1 or n2 or n3:
        cleaned = cleaned.strip(_EDGE_JUNK)
    return cleaned


_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>")
_JUNK_RE = re.compile(
    r"\b(s\d{1,2}e?\d{0,3}|season\s*\d+|ep(?:isode)?\.?\s*\d+|\d{3,4}p|4k|uhd|hdr\d*|"
    r"bluray|bdrip|remux|web-?dl|webrip|hdrip|dvdrip|hdtv|cam|hdts|ts|"
    r"hevc|x264|x265|avc|av1|aac|ddp\d?\.?\d?|dts|flac|mp3|ac3|"
    r"esubs?|subs?|subbed|dubbed|dual\s*audio|multi\s*audio|dual|multi|"
    + "|".join(pat for _code, pat, _tokens in _SOURCE_RULES) + "|"
    + "|".join(re.escape(a) for aliases in _LANGUAGE_ALIASES.values() for a in aliases)
    + r")\b.*",
    re.IGNORECASE,
)
_FIRST_WORD_RE = re.compile(r"^[\W_]*\w+")
_TITLE_PUNCT_RE = re.compile(r"[._\-+\[\]()'\"~]+")
_TITLE_SPACE_RE = re.compile(r"\s{2,}")


# Characters that are invisible or blank but still count as "word" characters
# (Hangul filler U+3164 is the common one in real captions: 1,300+ captions
# start with it) — they must never become the "first word" of a title.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u3164\u115f\u1160\u2800\u00a0]")
# "[TurabSSH] Beyblade S01E19" — a release-group tag in front of the title.
_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]\n]{1,40}\]\s*")
# "720p • Jawan 2023" — a quality tag in front of the title.
_LEADING_RES_RE = re.compile(r"^[\W_]*(?:\d{3,4}p|4k|uhd)\b[\W_]*", re.IGNORECASE)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]{2,}")  # a real word (2+ letters), not the "p" of 720p


def clean_title(text: str) -> str:
    # Captions are stored via Telegram's HTML rendering (message.caption.html),
    # so a bold/italic-formatted title arrives as literal "<b>Title</b>" —
    # strip that first, or a formatted caption's title never matches an
    # unformatted query, which used to make Stage 1 miss almost everything.
    t = _HTML_TAG_RE.sub(" ", text)
    t = clean_display_text(t)
    # Underscore-separated file names ("Avengers_Infinity_War_2018_BluRay…"):
    # "_" counts as a word character, so without this no year / quality tag in
    # them is ever recognised and the whole file name becomes the "title".
    t = _INVISIBLE_RE.sub(" ", t.replace("_", " ")).strip()

    # Junk IN FRONT of the title: a [Group] tag, then a leading 720p/1080p.
    stripped = _LEADING_TAG_RE.sub("", t, count=1)
    if _HAS_LETTER_RE.search(stripped):
        t = stripped
    for _ in range(2):
        stripped = _LEADING_RES_RE.sub("", t, count=1)
        if stripped == t or not _HAS_LETTER_RE.search(stripped):
            break
        t = stripped

    # A title can legitimately START with a language/tag word ("Hindi Medium",
    # "English Vinglish", "Sub Zero"). Keep the first word untouched and only
    # cut junk markers that come after it.
    head_match = _FIRST_WORD_RE.match(t)
    split = head_match.end() if head_match else 0
    junk = _JUNK_RE.search(t, split)
    stop = junk.start() if junk else len(t)

    # The release year is the most reliable end-of-title marker: in
    # "Jawan (2023) Extended Cut Bollywood Hindi ..." everything after it is
    # description, not title. Use the LAST year before the first quality /
    # language tag (so "Blade Runner 2049 (2017)" keeps "2049"), and only if
    # a real word stands in front of it (so "1917" and "2012" stay titles).
    cut = None
    for ym in _YEAR_PATTERN.finditer(t, 0, stop):
        if _HAS_LETTER_RE.search(t[:ym.start()]):
            cut = ym.start()
    if cut is not None:
        t = t[:cut]
    else:
        t = _YEAR_PATTERN.sub(" ", t)

    head_match = _FIRST_WORD_RE.match(t)
    if head_match:
        head, tail = t[:head_match.end()], t[head_match.end():]
        t = head + _JUNK_RE.sub(" ", tail)
    else:
        t = _JUNK_RE.sub(" ", t)
    t = _TITLE_PUNCT_RE.sub(" ", t)
    return _TITLE_SPACE_RE.sub(" ", t).strip(" \t•|:,;/\\")


# ── Matching key ────────────────────────────────────────────────────────────
# One normalised form of a title that is identical for every harmless way of
# writing it, so "Spider-Man", "Spiderman" and "Spider Man" — or "The Vvaan:
# Force of the Forrest" and "The Vvaan Force of Forrest" — all give the SAME
# key. Built the same way for stored files (save_file) and typed queries
# (search_files), so Stage 1 can match on one indexed equality instead of
# comparing text. What it ignores:
#   * spacing           spider man = spiderman = spider-man
#   * every symbol      : - – — , ! ? & / ' " . _ ( ) [ ] ...
#   * words listed in _KEY_STOPWORDS (empty on purpose: "the/a/an" count)
# What it never ignores: digits and every other word — "Weak Hero Class 2"
# never matches "Weak Hero Class 1", and "You" never matches "You Love Me".
# Bump this whenever the rules that build a title key change (clean_title /
# title_key). Files stored with an older number are re-keyed automatically by
# the startup backfill, so a rule change never leaves old files behind.
KEY_VERSION = 2
_KEY_STOPWORDS = frozenset()  # articles (the/a/an) are NOT dropped: "The Flash" and "Flash" are different titles
_KEY_WORD_RE = re.compile(r"[^\W_]+")
_KEY_APOSTROPHE_RE = re.compile(r"['\u2019\u2018`\u00b4]")
_KEY_POSSESSIVE_RE = re.compile(r"['\u2019\u2018`\u00b4]s\b", re.IGNORECASE)


def _key_of(text: str) -> str:
    t = clean_title(text).lower()
    return "".join(w for w in _KEY_WORD_RE.findall(t) if w not in _KEY_STOPWORDS)


def title_key_variants(text: str) -> list:
    """The key(s) a title can be found by. Usually one. A title with a
    possessive ("Mr. Bean's Holiday", "Schindler's List") gets two, because
    people type it both ways: "Beans" (apostrophe just removed) and "Bean"
    (the 's dropped)."""
    if not text:
        return []
    base = text.replace("&", " and ")
    keys = [_key_of(_KEY_APOSTROPHE_RE.sub("", base))]
    if _KEY_POSSESSIVE_RE.search(base):
        keys.append(_key_of(_KEY_APOSTROPHE_RE.sub("", _KEY_POSSESSIVE_RE.sub("", base))))
    return [k for k in dict.fromkeys(keys) if k]


def title_key(text: str) -> str:
    variants = title_key_variants(text)
    return variants[0] if variants else ""


def doc_title_keys(file_name: str, caption: str) -> list:
    """Every title key a stored file can be found by: from its caption (whole,
    and its first line when the caption has several) and from its file name."""
    caption = (caption or "").strip()
    sources = [caption, file_name or ""]
    if "\n" in caption:
        sources.insert(1, caption.split("\n", 1)[0])
    return sorted({k for src in sources for k in title_key_variants(src)})


def extract_meta(results: list) -> dict:
    """One pass over the full (unfiltered) result set. Returns the distinct
    filter values available, so filter buttons never offer an empty choice."""
    seasons, episodes, years = set(), set(), set()
    languages = set()
    qualities: dict = {}  # code -> label

    for doc in results:
        text = _doc_text(doc)
        season = _season_of(text)
        if season:
            seasons.add(season)
        episode = _episode_of(text)
        if episode:
            episodes.add(episode)
        year = _year_of(text)
        if year:
            years.add(year)
        languages.update(_languages_of(text))
        code, label = _quality_of(text)
        if code:
            qualities[code] = label

    quality_order = [code for _pat, code, _label in _QUALITY_RULES]
    ordered_qualities = [(c, qualities[c]) for c in quality_order if c in qualities]

    return {
        "seasons": sorted(seasons),
        "episodes": sorted(episodes),
        "years": sorted(years, reverse=True),
        "languages": sorted(languages),
        "qualities": ordered_qualities,
    }


def apply_filters(results: list, filters: dict) -> list:
    """AND-combine every active filter over the full result set."""
    if not filters:
        return results

    season = filters.get("season")
    episode = filters.get("episode")
    year = filters.get("year")
    quality = filters.get("quality")
    language = filters.get("language")
    resolution = filters.get("resolution")
    source = filters.get("source")

    out = []
    for doc in results:
        text = _doc_text(doc)
        if season and _season_of(text) != int(season):
            continue
        if episode and _episode_of(text) != int(episode):
            continue
        if year and _year_of(text) != str(year):
            continue
        if quality and _quality_of(text)[0] != quality:
            continue
        if language and not _language_matches(text, language):
            continue
        if resolution and not _resolution_matches(text, resolution):
            continue
        if source and not _source_matches(text, source):
            continue
        out.append(doc)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════════════════════════

_MAX_FETCH = 200          # hard ceiling on results returned per query
_CANDIDATE_FETCH = 3000   # how many word-index matches to pull before the
                           # exact-title re-check narrows them down — the
                           # final check is a cheap string compare, so this
                           # can afford to be generous

# Tag-extraction patterns match ONLY at the very end of the (remaining)
# query, one tag at a time, never touching the start/middle — this is what
# keeps a title that legitimately contains a language/quality-sounding word
# ("Hindi Medium", "1917") intact, while still pulling out tags a user
# actually appended ("... 1080p Hindi"). "Season 1"/"Season1"/"episode 3"/
# "ep3" are all recognised regardless of spacing (\s* matches zero spaces).
_TRAILING_SE_RE = re.compile(
    r"^(?P<title>.*\S)\s+S(?P<season>\d{1,2})E(?P<episode>\d{1,3})\s*$", re.IGNORECASE,
)
_TRAILING_SEASON_RE = re.compile(
    r"^(?P<title>.*\S)\s+(?:S(?P<season1>\d{1,2})|Season\s*(?P<season2>\d{1,2}))\s*$", re.IGNORECASE,
)
_TRAILING_EPISODE_RE = re.compile(
    r"^(?P<title>.*\S)\s+(?:Episode|Ep)\.?\s*(?P<episode>\d{1,3})\s*$", re.IGNORECASE,
)
_TRAILING_YEAR_RE = re.compile(r"^(?P<title>.*\S)\s+(?P<year>19[5-9]\d|20[0-3]\d)\s*$")
_TRAILING_RESOLUTION_RE = re.compile(
    r"^(?P<title>.*\S)\s+(?P<resolution>"
    + "|".join(pat for _code, pat in _RESOLUTION_RULES) + r")\s*$",
    re.IGNORECASE,
)
_TRAILING_SOURCE_RE = re.compile(
    r"^(?P<title>.*\S)\s+(?P<source>"
    + "|".join(pat for _code, pat, _tokens in _SOURCE_RULES) + r")\s*$",
    re.IGNORECASE,
)
_ALL_LANG_ALIASES = sorted(
    {alias for aliases in _LANGUAGE_ALIASES.values() for alias in aliases},
    key=len, reverse=True,
)
_TRAILING_LANG_RE = re.compile(
    r"^(?P<title>.*\S)\s+(?P<lang>" + "|".join(re.escape(a) for a in _ALL_LANG_ALIASES) + r")\s*$",
    re.IGNORECASE,
)

_TRAILING_PATTERNS = [
    ("se", _TRAILING_SE_RE),
    ("season", _TRAILING_SEASON_RE),
    ("episode", _TRAILING_EPISODE_RE),
    ("year", _TRAILING_YEAR_RE),
    ("resolution", _TRAILING_RESOLUTION_RE),
    ("source", _TRAILING_SOURCE_RE),
    ("language", _TRAILING_LANG_RE),
]


def parse_query(query: str) -> tuple:
    """
    Split a raw user query into (title, tags) by repeatedly peeling a
    recognised tag off the END of the query only. Returns the untouched
    title (same words, same order, same spelling the user typed) plus
    whichever of season/episode/year/quality/language were found trailing
    it. Never strips a query down to nothing — a query that IS just "1917"
    stays a title, not a year.
    `resolution` (480p/720p/1080p/...) and `source` (WEB-DL/HDRip/HDTC/HD/...)
    come back as lists of codes (or None).
    """
    title = re.sub(r"\s+", " ", query.strip())
    tags = {"season": None, "episode": None, "year": None, "language": None,
            "resolution": None, "source": None}
    if not title:
        return title, tags

    progress = True
    while progress:
        progress = False
        for kind, pattern in _TRAILING_PATTERNS:
            m = pattern.match(title)
            if not m:
                continue
            candidate = m.group("title").strip()
            if not candidate:
                continue  # would empty the title out — refuse and try nothing else this round

            if kind == "se":
                tags["season"] = tags["season"] if tags["season"] is not None else int(m.group("season"))
                tags["episode"] = tags["episode"] if tags["episode"] is not None else int(m.group("episode"))
            elif kind == "season":
                num = m.group("season1") or m.group("season2")
                tags["season"] = tags["season"] if tags["season"] is not None else int(num)
            elif kind == "episode":
                tags["episode"] = tags["episode"] if tags["episode"] is not None else int(m.group("episode"))
            elif kind == "year":
                tags["year"] = tags["year"] or m.group("year")
            elif kind == "resolution":
                code = _resolution_code_of(m.group("resolution"))
                if code and code not in (tags["resolution"] or []):
                    tags["resolution"] = (tags["resolution"] or []) + [code]
            elif kind == "source":
                code = _source_code_of(m.group("source"))
                if code and code not in (tags["source"] or []):
                    tags["source"] = (tags["source"] or []) + [code]
            elif kind == "language":
                langs = _languages_of(m.group("lang"))
                if langs and not tags["language"]:
                    tags["language"] = langs[0]

            title = candidate
            progress = True
            break  # restart the pattern list against the now-shorter title

    return title, tags


# ══════════════════════════════════════════════════════════════════════════════
# TAG-ONLY QUERIES — e.g. "Hindi", "2024", "Hindi 2024", "1080p", "Hindi 2024
# 720p WEB-DL". There is no title in such a query, so the exact-title search
# below has nothing to compare and used to return nothing. Here the tags ARE
# the search: every file whose name/caption carries all of them is returned.
# Only queries made up purely of language / year / resolution / source tags
# are treated this way — anything else goes through the normal title search
# exactly as before.
# ══════════════════════════════════════════════════════════════════════════════

def parse_tag_only_query(query: str):
    """Return {"languages", "year", "resolution", "source"} if the query
    consists ONLY of tags, else None. languages/resolution/source are lists
    of codes (maybe empty), year is a string or None."""
    text = re.sub(r"\s+", " ", (query or "").lower()).strip()
    if not text:
        return None

    sources, resolutions, languages = [], [], []
    for code, _pat, _tokens in _SOURCE_RULES:
        text, n = _SOURCE_TEXT_PATTERNS[code].subn(" ", text)
        if n:
            sources.append(code)
    for code, _pat in _RESOLUTION_RULES:
        text, n = _RES_TEXT_PATTERNS[code].subn(" ", text)
        if n:
            resolutions.append(code)
    for canonical, pat in _LANG_ALIAS_PATTERNS.items():
        text, n = pat.subn(" ", text)
        if n:
            languages.append(canonical)
    years = set(_YEAR_PATTERN.findall(text))
    text = _YEAR_PATTERN.sub(" ", text)

    if _tokenize(text):
        return None  # a real title word is present — not a tag-only query
    if len(years) > 1:
        return None  # two different years — ambiguous, leave to normal flow
    year = next(iter(years), None)
    if not (sources or resolutions or languages or year):
        return None
    return {"languages": languages, "year": year, "resolution": resolutions, "source": sources}


def is_language_year_query(query: str) -> bool:
    """True for a query that is only language / year / resolution / quality tags."""
    return parse_tag_only_query(query) is not None


async def _search_by_tags(query: str) -> list:
    parsed = parse_tag_only_query(query)
    if not parsed:
        return []
    languages, year = parsed["languages"], parsed["year"]
    resolutions, sources = parsed["resolution"], parsed["source"]

    # The `words` index narrows the candidates; the same regex helpers the
    # filter buttons use then make the final decision, so results here always
    # agree with them.
    conditions = []
    if year:
        conditions.append({"words": year})
    for language in languages:
        tokens = [a for a in _LANGUAGE_ALIASES[language] if " " not in a]
        conditions.append({"words": {"$in": tokens}})
    if resolutions:
        tokens = []
        for code, pat in _RESOLUTION_RULES:
            if code in resolutions:
                tokens.extend(_tokenize(pat.replace("|", " ")))
        conditions.append({"words": {"$in": sorted(set(tokens))}})
    for code in sources:
        alternatives = [
            {"words": alt[0]} if len(alt) == 1 else {"words": {"$all": alt}}
            for alt in _SOURCE_TOKENS[code]
        ]
        conditions.append(alternatives[0] if len(alternatives) == 1 else {"$or": alternatives})
    mongo_query = conditions[0] if len(conditions) == 1 else {"$and": conditions}

    cursor = files.find(mongo_query, _RESULT_FIELDS).limit(_CANDIDATE_FETCH)
    candidates = await cursor.to_list(length=_CANDIDATE_FETCH)

    results = []
    for doc in candidates:
        text = _doc_text(doc)
        if year and _year_of(text) != year:
            continue
        if not all(_language_matches(text, language) for language in languages):
            continue
        if resolutions and not _resolution_matches(text, resolutions):
            continue
        if sources and not _source_matches(text, sources):
            continue
        results.append(doc)

    results.sort(key=lambda d: -_quality_rank(_doc_text(d)))
    return results[:_MAX_FETCH]


async def related_titles(query: str, limit: int = 8) -> list:
    """
    Titles in the database that START WITH the query — used only when there is
    no exact match, so "pushpa" can offer "Pushpa The Rise" and "dune" can
    offer "Dune Part Two". Returns clean display titles, most files first.

    Matching is on whole words ("jawan" never offers "Jawani ..."), ignores
    spacing/symbols like the normal search ("spiderman" offers "Spider Man No
    Way Home"), and only returns titles that are LONGER than the query — an
    equal title would already have been found by the normal search.
    """
    title = parse_query(query)[0]
    qkeys = title_key_variants(title)
    if not qkeys or min(len(k) for k in qkeys) < 2:
        return []

    # tkeys is indexed, and an anchored prefix regex can use that index.
    cond = [{"tkeys": {"$regex": "^" + re.escape(k)}} for k in qkeys]
    cursor = files.find({"$or": cond}, {"file_name": 1, "caption": 1}).limit(_CANDIDATE_FETCH)
    docs = await cursor.to_list(length=_CANDIDATE_FETCH)

    longest = max(len(k) for k in qkeys)
    groups: dict = {}  # normalised title -> {"count": n, "shown": {display: n}}
    for doc in docs:
        seen = set()
        for src in {display_name(doc), doc.get("file_name", "") or ""}:
            shown = clean_title(src)
            tokens = _KEY_WORD_RE.findall(_KEY_APOSTROPHE_RE.sub("", shown).lower())
            acc = ""
            for i, tok in enumerate(tokens):
                acc += tok
                if acc in qkeys:
                    if i + 1 < len(tokens):  # strictly longer than the query
                        norm = " ".join(tokens)
                        if norm not in seen:
                            seen.add(norm)
                            g = groups.setdefault(norm, {"count": 0, "shown": {}})
                            g["count"] += 1
                            g["shown"][shown] = g["shown"].get(shown, 0) + 1
                    break
                if len(acc) >= longest:
                    break

    ranked = sorted(groups.items(), key=lambda kv: (-kv[1]["count"], len(kv[0])))
    out = []
    for _norm, g in ranked[:limit]:
        out.append(max(g["shown"].items(), key=lambda kv: kv[1])[0])
    return out


async def search_files(query: str, use_alias: bool = True) -> list:
    """
    Stage 1 search. Deliberately simple and strict:
      1. Pull any trailing season/episode/year/quality/language tag off the
         query (never touching the title itself).
      2. Reduce the remaining title to its clean form (same cut used on
         database captions) and require an EXACT match — not "contains
         these words", a full match — so a short query like "You" only
         ever matches a file whose title genuinely IS "You", never "You
         Love Me"; and "Weak Hero Class 2" never matches a file that's
         actually "Weak Hero Class 1". A word-index lookup narrows the
         candidates first purely for speed; the exact-title check is what
         actually decides the result, so the narrowing step can never let
         a wrong file through on its own.
      3. Hard-filter by whatever tags were pulled out in step 1.
    Callers paginate the returned list in memory — one DB round trip per
    query, not one per page.
    """
    query = query.strip()
    if not query:
        return []

    # A query made only of tags (language / year / resolution / quality) is a
    # tag search, decided before any title cleaning so a language word at the
    # start of a title can never be mistaken for it (or vice versa).
    if parse_tag_only_query(query) is not None:
        return await _search_by_tags(query)

    title, tags = parse_query(query)
    query_key = clean_title(title).lower()
    if not query_key:
        # No title left to match — if the query is just tags (language, year,
        # resolution, quality), search by those instead of returning nothing.
        return await _search_by_tags(query)

    key_source = title  # raw title text (keeps apostrophes for the key variants)
    if use_alias:
        # Admin-approved alias ("hindi madium" -> "hindi medium"). Only the
        # title is swapped; tags from the query are still applied below.
        # Imported here because alias_db itself imports this module.
        from database.alias_db import get_alias
        aliased = await get_alias(query_key)
        if aliased:
            query_key = key_source = aliased

    # Primary path: one indexed lookup on the spacing/symbol-proof title key
    # ("spiderman" = "spider man" = "Spider-Man", "force of the forrest" =
    # "force of forrest"). Files not yet migrated to tkeys fall through to the
    # original word-index + exact clean-title path below.
    results = []
    qkeys = title_key_variants(key_source)
    if qkeys:
        cursor = files.find({"tkeys": {"$in": qkeys}}, _RESULT_FIELDS).limit(_CANDIDATE_FETCH)
        results = await cursor.to_list(length=_CANDIDATE_FETCH)

    if not results:
        words = _tokenize(query_key)
        if not words:
            return []
        cursor = files.find({"words": {"$all": words}}, _RESULT_FIELDS).limit(_CANDIDATE_FETCH)
        candidates = await cursor.to_list(length=_CANDIDATE_FETCH)
        results = [doc for doc in candidates if clean_title(display_name(doc)).lower() == query_key]

    active_tags = {k: v for k, v in tags.items() if v is not None}
    if active_tags:
        results = apply_filters(results, active_tags)

    results.sort(key=lambda d: -_quality_rank(_doc_text(d)))
    return results[:_MAX_FETCH]


async def get_file_by_id(object_id: str) -> dict | None:
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        oid = ObjectId(object_id)
    except InvalidId:
        return None
    return await files.find_one({"_id": oid})
