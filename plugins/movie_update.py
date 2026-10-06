"""
Movie Update Notification Plugin

Features:
1. Manual /m command for admins to send movie update notifications
2. Automatic monitoring of fetch channels (set in /settings -> Movie Updates)
   for file uploads
3. Uses existing database search for accurate file matching
4. Different button handling: bot query search for manual, channel links for automatic
"""
import io
import re
import html
import asyncio
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional, Tuple

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified

from config import (
    MOVIE_UPDATE_CHANNEL,
    LINK_PREVIEW, ABOVE_PREVIEW, TMDB_POSTER, LANDSCAPE_POSTER, ADMINS
)
from database.movie_update_db import movie_update_db
from database.filters_db import search_files
from movie_metadata import get_movie_details, get_movie_detailsx
from poster import fetch_poster
from strings import MOVIE_UPDATE_NOTIFY_TXT, MANUAL_UPDATE_NOTIFY_TXT
from utils import temp
import aiohttp

logger = logging.getLogger(__name__)

# Media filter for catching file uploads
media_filter = filters.document | filters.video | filters.audio

# ══════════════════════════════════════════════════════════════════════════════
# Filename / caption parsing
# ══════════════════════════════════════════════════════════════════════════════

CLEAN_PATTERN = re.compile(
    r'@\w+|https?://\S+|\bwww\.[a-z0-9\-]+\.[a-z]{2,4}|\b[a-z0-9\-]+\.(?:com|net|org|cc|ws|xyz|vip|club|lol|ink|link)\b|#\S+',
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>")
_YEAR_TOKEN_RE = re.compile(r'(?:19[5-9]\d|20[0-3]\d)')

OTT_PLATFORMS = {
    'netflix': 'Netflix', 'prime': 'Prime Video', 'disney': 'Disney+',
    'hulu': 'Hulu', 'hbo': 'HBO Max', 'apple': 'Apple TV+',
    'paramount': 'Paramount+', 'peacock': 'Peacock',
    'zee5': 'ZEE5', 'sonyliv': 'SonyLIV',
}
# Scene/short tags that mean the same platform. Matched as WHOLE words only,
# so a title like "Optimus Prime" or "Apple of My Eye" no longer tags an OTT.
_OTT_ALIASES = {
    'netflix': 'netflix', 'nf': 'netflix',
    'amzn': 'prime', 'amazon': 'prime', 'primevideo': 'prime', 'prime': 'prime',
    'disney': 'disney', 'dsnp': 'disney', 'hotstar': 'disney', 'jiohotstar': 'disney',
    'hulu': 'hulu',
    'hbo': 'hbo', 'hmax': 'hbo',
    'atvp': 'apple', 'appletv': 'apple',
    'paramount': 'paramount', 'pmtp': 'paramount',
    'peacock': 'peacock', 'pcok': 'peacock',
    'zee5': 'zee5', 'sonyliv': 'sonyliv',
}
_OTT_RE = re.compile(r'\b(' + '|'.join(sorted(_OTT_ALIASES, key=len, reverse=True)) + r')\b', re.IGNORECASE)

# Language patterns
CAPTION_LANGUAGES = {
    'hin': 'Hindi', 'eng': 'English', 'tel': 'Telugu', 'tam': 'Tamil',
    'kan': 'Kannada', 'mal': 'Malayalam', 'ben': 'Bengali', 'mar': 'Marathi',
    'guj': 'Gujarati', 'pun': 'Punjabi', 'urd': 'Urdu', 'kor': 'Korean',
    'jap': 'Japanese',
}
_LANG_ALIAS = {}
for _code, _name in CAPTION_LANGUAGES.items():
    _LANG_ALIAS[_code] = _code
    _LANG_ALIAS[_name.lower()] = _code
_LANG_RE = re.compile(r'\b(' + '|'.join(sorted(_LANG_ALIAS, key=len, reverse=True)) + r')\b', re.IGNORECASE)

# Source / quality tags → canonical label (so "Bluray", "BluRay", "BLURAY" and
# "Web-DL"/"WEB DL"/"webdl" all collapse to one entry when files are merged).
_SOURCE_TAGS = [
    ("WEB-DL", r"webdl"),
    ("WEBRip", r"webrip"),
    ("BluRay", r"bluray|brrip"),
    ("BDRip", r"bdrip"),
    ("HDRip", r"hdrip"),
    ("DVDRip", r"dvdrip"),
    ("DVDScr", r"dvdscr"),
    ("HDTV", r"hdtv"),
    ("HDCAM", r"hdcam|camrip|cam"),
    ("HDTS", r"hdts|hdtc"),
    ("PreDVD", r"predvd"),
]
_EXTRA_TAGS = [
    ("REMUX", r"remux"),
    ("UHD", r"uhd"),
    ("HDR", r"hdr\d*"),
]
_RES_RE = re.compile(r'\b(480p|576p|720p|1080p|1440p|2160p|4k)\b', re.IGNORECASE)

# Tokens that mark the END of the title inside a release name.
_STOP_WORDS = {
    # sources
    'webdl', 'webrip', 'web', 'bluray', 'brrip', 'bdrip', 'hdrip', 'dvdrip', 'dvdscr', 'hdtv',
    'hdcam', 'camrip', 'hdts', 'hdtc', 'predvd', 'preweb', 'remux', 'uhd',
    # codecs / audio
    'x264', 'x265', 'h264', 'h265', 'hevc', 'avc', '10bit', '8bit', 'aac', 'ac3',
    'ddp', 'dd', 'dts', 'truehd', 'atmos',
    # containers
    'mkv', 'mp4', 'avi', 'mov',
    # subtitles / release phrases
    'esub', 'esubs', 'esubd', 'msubs', 'subs', 'sub', 'subbed',
    'dubbed', 'dual', 'multi', 'untouched', 'proper', 'repack', 'season',
    # languages
    'hindi', 'english', 'tamil', 'telugu', 'kannada', 'malayalam', 'bengali',
    'marathi', 'gujarati', 'punjabi', 'urdu', 'korean', 'japanese',
    # platforms (scene tags only — never plain words like "prime"/"apple")
    'nf', 'amzn', 'dsnp', 'hotstar', 'zee5', 'sonyliv', 'netflix', 'hulu',
}
_STOP_RE = re.compile(
    r'(?:19[5-9]\d|20[0-3]\d)'            # year
    r'|\d{3,4}p|4k|hdr\d*'                # resolution / HDR
    r'|s\d{1,2}(?:e(?:p)?\d{1,3}.*)?'     # S02 / S02E05
    r'|ep?\d{2,3}|ep\d+'                  # E05 / EP5
    r'|\d{1,2}x\d{2,3}',                  # 2x05
    re.IGNORECASE,
)

# Lock management for concurrent updates
locks = {}
pending_updates = {}


def clean_mentions_links(text: str) -> str:
    return CLEAN_PATTERN.sub(" ", text or "").strip()


def normalize(s: str) -> str:
    s = re.sub(r'[^\w\s]', " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _plain(text: str) -> str:
    """Caption text as stored in the DB is Telegram HTML — drop the tags."""
    return _HTML_TAG_RE.sub(" ", text or "")


def _prep(text: str) -> str:
    """Make a raw filename/caption uniform: no @mentions/links/#tags, no
    apostrophes, dots/underscores/brackets as spaces, and 'Web-DL' / 'Blu-ray'
    collapsed to single tokens. Hyphens are KEPT (episode ranges need them)."""
    text = clean_mentions_links(text)
    text = re.sub(r"[\u2018\u2019'`]", "", text)
    # Leading [site.com] / (group) prefixes are never part of the title.
    while True:
        m = re.match(r'^\s*[\[\(\{]([^\]\)\}]*)[\]\)\}]\s*', text)
        if not m or _YEAR_TOKEN_RE.fullmatch(m.group(1).strip()):
            break
        text = text[m.end():]
    text = re.sub(r"[._\[\]\(\)\{\}]+", " ", text)
    text = re.sub(r"\bweb[\s\-]*dl\b", "webdl", text, flags=re.IGNORECASE)
    text = re.sub(r"\bweb[\s\-]*rip\b", "webrip", text, flags=re.IGNORECASE)
    text = re.sub(r"\bblu[\s\-]*ray\b", "bluray", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _tag_list(text: str, table) -> list:
    text = text.lower()
    return [label for label, pat in table if re.search(rf"\b(?:{pat})\b", text)]


def get_source_quality(text: str) -> str:
    """Return source/format tags only: WEB-DL, WEBRip, BluRay, HDRip, etc."""
    found = _tag_list(_prep(text), _SOURCE_TAGS)
    return ", ".join(found) if found else "N/A"


def get_qualities(text: str) -> str:
    """Return extra quality tags only: REMUX, UHD, HDR."""
    found = _tag_list(_prep(text), _EXTRA_TAGS)
    return ", ".join(found) if found else "N/A"


def get_resolution(text: str) -> str:
    """Return resolution tags only: 720p, 1080p, 4K, etc."""
    seen, result = set(), []
    for r in _RES_RE.findall(_prep(text)):
        r = r.lower()
        label = "4K" if r in ("4k", "2160p") else r
        if label not in seen:
            seen.add(label)
            result.append(label)
    return ", ".join(result) if result else "N/A"


def _match_languages(text: str) -> list:
    """Language codes found in text — whole words only ('marvel' is not
    Marathi, 'thing' is not Hindi)."""
    text = re.sub(r"[_]+", " ", (text or "").lower())
    found = []
    for m in _LANG_RE.finditer(text):
        code = _LANG_ALIAS[m.group(1).lower()]
        if code not in found:
            found.append(code)
    return found


def _match_ott_keys(text: str) -> list:
    """Match OTT platform keys in text (whole words only)."""
    text = re.sub(r"[_]+", " ", (text or "").lower())
    found = []
    for m in _OTT_RE.finditer(text):
        key = _OTT_ALIASES[m.group(1).lower()]
        if key not in found:
            found.append(key)
    return found


def extract_ott_platform(text: str) -> str:
    platforms = sorted({OTT_PLATFORMS[k] for k in _match_ott_keys(text)})
    return " | ".join(platforms) if platforms else "N/A"


# ── season / episode ─────────────────────────────────────────────────────────
_EP = r'e(?:p)?\s*\d{1,3}'
_EP_LIST = (
    rf'{_EP}(?:\s*(?:(?:-|~|&|to)\s*)?{_EP}|\s*(?:-|~|&|to)\s*\d{{1,3}})*'
)
_SE_EP_RE = re.compile(rf'\bs(\d{{1,2}})\s*({_EP_LIST})\b', re.IGNORECASE)
_SEASON_WORD_EP_RE = re.compile(
    r'\bseason\s*(\d{1,2})\s*(?:episode|ep|e)\s*(\d{1,3})(?:\s*(?:-|~|to)\s*(\d{1,3}))?\b', re.IGNORECASE)
_NXN_RE = re.compile(r'\b(\d{1,2})x(\d{2,3})\b', re.IGNORECASE)
_SEASON_ONLY_RE = re.compile(r'\b(?:s(\d{1,2})|season\s*(\d{1,2}))\b', re.IGNORECASE)
_EP_ONLY_RE = re.compile(
    r'\b(?:ep(?:isode)?\s*(\d{1,3})(?:\s*(?:-|~|to)\s*(\d{1,3}))?|e(\d{2,3}))\b', re.IGNORECASE)


def _ep_str(nums) -> str:
    nums = [int(n) for n in nums if n is not None and str(n) != ""]
    if not nums:
        return "Complete"
    lo, hi = nums[0], nums[-1]
    return str(lo) if lo == hi else f"{lo}-{hi}"


def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[str]]:
    """(season, episode) from S02E05 / S02E05-10 / S02E05E06 / Season 2 Episode 5 /
    2x05 / S02 (→ 'Complete') / EP05 (→ season 1). (None, None) for movies."""
    text = _prep(filename)
    if m := _SE_EP_RE.search(text):
        return int(m.group(1)), _ep_str(re.findall(r'\d+', m.group(2)))
    if m := _SEASON_WORD_EP_RE.search(text):
        return int(m.group(1)), _ep_str([m.group(2), m.group(3)])
    if m := _NXN_RE.search(text):
        return int(m.group(1)), _ep_str([m.group(2)])
    if m := _SEASON_ONLY_RE.search(text):
        return int(m.group(1) or m.group(2)), "Complete"
    if m := _EP_ONLY_RE.search(text):
        return 1, _ep_str([m.group(1) or m.group(3), m.group(2)])
    return None, None


# ── title extraction ─────────────────────────────────────────────────────────
def _is_stop_token(tok: str) -> bool:
    t = tok.lower()
    return t in _STOP_WORDS or bool(_STOP_RE.fullmatch(t))


def _split_title(text: str):
    """Split a prepped release name into (title_tokens, tail_tokens). The
    title ends at the first release tag (year, resolution, source, codec,
    language, S01E02 …). The first word is always kept as title, so names
    like 'Hindi Medium' or '1917' survive."""
    tokens = text.replace("-", " ").split()
    cut = len(tokens)
    for i, tok in enumerate(tokens):
        if i == 0 or not _is_stop_token(tok):
            continue
        # "Blade Runner 2049 2017 …": a year directly followed by another
        # year is part of the title.
        if _YEAR_TOKEN_RE.fullmatch(tok) and i + 1 < len(tokens) and _YEAR_TOKEN_RE.fullmatch(tokens[i + 1]):
            continue
        cut = i
        break
    return tokens[:cut], tokens[cut:]


def _title_case(words: list) -> str:
    title = " ".join(w for w in words if re.search(r'\w', w))
    if title.isupper() or title.islower():
        return title.title()
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in title.split())


def extract_media_info(filename: str, caption: str):
    filename = filename or ""
    prepped = _prep(filename)
    caption_prepped = _prep(_plain(caption))

    title_tokens, tail_tokens = _split_title(prepped)
    if not normalize(" ".join(title_tokens)):
        # No usable filename (videos often have none) — use the caption's first line.
        first_line = next((ln for ln in _plain(caption).splitlines() if ln.strip()), "")
        title_tokens, tail_tokens = _split_title(_prep(first_line))
        tail_tokens = tail_tokens + prepped.split()

    title = normalize(" ".join(title_tokens).replace("-", " "))
    # Trailing filler words that are never part of a title.
    title = re.sub(r'(?:\s+(?:movie|movies|full|series|web series|tv series))+$', '', title, flags=re.IGNORECASE) or title
    display_name = _title_case(title.split())
    base_name = display_name.lower()

    tail = " ".join(tail_tokens)
    title_words = {w.lower() for w in title_tokens}
    caption_extra = " ".join(w for w in caption_prepped.split() if w.lower() not in title_words)
    # Filename tail first, caption only adds to it.
    detect = f"{tail} {caption_extra}".strip()

    year_m = _YEAR_TOKEN_RE.search(" ".join(t for t in tail_tokens if _YEAR_TOKEN_RE.fullmatch(t)))
    if not year_m:
        year_m = re.search(r'\b(19[5-9]\d|20[0-3]\d)\b', caption_extra)
    year = year_m.group(0) if year_m else None

    source = get_source_quality(detect)
    extras = get_qualities(detect)
    quality_parts = [p for p in (source, extras) if p != "N/A"]
    final_quality = ", ".join(quality_parts) if quality_parts else "N/A"
    resolution = get_resolution(detect)

    lang_keys = _match_languages(detect)
    language = ", ".join(CAPTION_LANGUAGES[k] for k in lang_keys) if lang_keys else "N/A"
    ott_platform = extract_ott_platform(detect)

    season, episode = extract_season_episode(tail)
    if season is None:
        season, episode = extract_season_episode(caption_extra)

    tag = "#SERIES" if season else "#MOVIE"

    return {
        "base_name": base_name,
        "display_name": display_name,
        "processed": f"{prepped} {caption_prepped}".strip(),
        "year": year,
        "quality": final_quality,
        "resolution": resolution,
        "language": language,
        "ott_platform": ott_platform,
        "season": season,
        "episode": episode,
        "tag": tag,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Notification text
# ══════════════════════════════════════════════════════════════════════════════

def _res_sort_key(label: str):
    label = label.lower()
    if label == "4k":
        return 2160
    m = re.match(r'(\d+)', label)
    return int(m.group(1)) if m else 0


def _format_episodes(episodes_by_season: dict) -> str:
    """{'1': {'1','2','3','5'}, ...} → '📺 ᴇᴘɪsᴏᴅᴇs : <b>S1: 1-3, 5</b>' (or '')."""
    if not episodes_by_season:
        return ""
    episode_lines = []
    for s_key, episodes in sorted(episodes_by_season.items(), key=lambda x: int(x[0])):
        singles, ranges = [], []
        for ep in episodes:
            if "-" in ep:
                ranges.append(ep)
            else:
                try:
                    singles.append(int(ep))
                except ValueError:
                    ranges.append(ep)   # "Complete"
        singles.sort()
        collapsed = []
        start = end = None
        for num in singles:
            if start is None:
                start = end = num
            elif num == end + 1:
                end = num
            else:
                collapsed.append(str(start) if start == end else f"{start}-{end}")
                start = end = num
        if start is not None:
            collapsed.append(str(start) if start == end else f"{start}-{end}")
        ranges.sort(key=lambda s: int(s.split("-")[0]) if s.split("-")[0].isdigit() else 10**6)
        episode_lines.append(f"S{int(s_key)}: {', '.join(collapsed + ranges)}")
    return f"📺 ᴇᴘɪsᴏᴅᴇs : <b>{html.escape(' | '.join(episode_lines))}</b>"


def generate_movie_message(movie_doc, base_name):
    """Generate movie update message text."""
    all_qualities = set()
    all_resolutions = set()
    all_languages = set()
    all_ott_platforms = set()
    episodes_by_season = defaultdict(set)

    for f in movie_doc.get("files", []):
        if q := f.get("quality"):
            if q != "N/A":
                all_qualities.update(x.strip() for x in q.split(",") if x.strip())
        if r := f.get("resolution"):
            if r != "N/A":
                all_resolutions.update(x.strip() for x in r.split(",") if x.strip())
        if l := f.get("language"):
            if l != "N/A":
                all_languages.update(x.strip() for x in l.split(",") if x.strip())
        if o := f.get("ott_platform"):
            if o != "N/A":
                all_ott_platforms.update(p.strip() for p in o.split("|") if p.strip())

        season = f.get("season")
        episode = f.get("episode")
        if season and episode:
            episodes_by_season[str(season)].add(str(episode))

    quality_str = ", ".join(sorted(all_qualities)) or "N/A"
    resolution_str = ", ".join(sorted(all_resolutions, key=_res_sort_key)) or "N/A"
    language_str = ", ".join(sorted(all_languages)) or "N/A"
    ott_str = " | ".join(sorted(all_ott_platforms)) or "N/A"
    epi_block = _format_episodes(episodes_by_season)

    rating = movie_doc.get("rating", "N/A")
    genres = movie_doc.get("genres", "N/A")
    tag = movie_doc.get("tag", "#MOVIE")
    filename = movie_doc.get("display_name") or base_name.title()

    e = html.escape
    return MOVIE_UPDATE_NOTIFY_TXT.format(
        tag=tag,
        filename=e(str(filename)),
        genres=e(str(genres)),
        ott=e(ott_str),
        quality=e(quality_str),
        resolution=e(resolution_str),
        language=e(language_str),
        rating=e(str(rating)),
        episodes=epi_block
    )


# ══════════════════════════════════════════════════════════════════════════════
# Sending / editing
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_image(url: str, size: tuple = None) -> Optional[io.BytesIO]:
    """Download poster bytes and wrap them in a named BytesIO — Pyrogram's
    send_photo cannot take raw `bytes`, only a path / URL / file object.
    Retries once: TMDB's image CDN occasionally times out on a cold fetch."""
    for attempt in range(2):
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            buf = io.BytesIO(data)
                            buf.name = "poster.jpg"
                            return buf
                    else:
                        logger.warning("Poster download got HTTP %s for %s", response.status, url)
        except Exception as e:
            logger.warning("Poster download failed (attempt %d): %s", attempt + 1, e)
        await asyncio.sleep(1)
    return None


async def resolve_poster_url(title: str, year=None, details: dict = None, used_tmdb: bool = False) -> Optional[str]:
    """Poster for a notification — found the SAME way as for a user's search
    (poster.fetch_poster: TMDB backdrop → TMDB poster → OMDb), with the richer
    metadata lookup only as a fallback. LANDSCAPE_POSTER=false prefers the
    portrait image from the metadata lookup instead."""
    details = details or {}
    query = f"{title} {year}" if year else title

    if not LANDSCAPE_POSTER and details.get("poster_url"):
        return details["poster_url"]

    for attempt in range(2):                       # one retry on a transient miss
        poster = await fetch_poster(query)
        if poster and poster.get("url"):
            return poster["url"]
        if attempt == 0:
            await asyncio.sleep(1.5)

    if used_tmdb and LANDSCAPE_POSTER and details.get("backdrop_url"):
        return details["backdrop_url"]
    return details.get("poster_url") or details.get("backdrop_url")


def _build_markup(movie_doc) -> InlineKeyboardMarkup:
    """One 'get file' button per source channel (first link seen for each)."""
    links = {}
    for f in movie_doc.get("files", []):
        link = f.get("source_channel")
        if not link:
            continue
        links.setdefault(f.get("source_chat_id", link), link)
    buttons = [[InlineKeyboardButton("✨ ɢᴇᴛ ᴅɪʀᴇᴄᴛ ꜰɪʟᴇ ✨", url=link)] for link in sorted(links.values())]
    buttons.append([InlineKeyboardButton("♨️ Viral Stuff ♨️", url="https://t.me/Reload_adultbot")])
    return InlineKeyboardMarkup(buttons)


def _text_body(text: str, poster_url: Optional[str]):
    """Text-mode body. With LINK_PREVIEW the poster is shown as a link preview
    (an invisible link at the start of the text)."""
    if poster_url and LINK_PREVIEW:
        return f'<a href="{html.escape(poster_url, quote=True)}">\u200b</a>{text}', True
    return text, False


async def _post_notification(bot, text, reply_markup, poster_url):
    """Post to MOVIE_UPDATE_CHANNEL. Returns (message, is_photo)."""
    if not MOVIE_UPDATE_CHANNEL:
        raise RuntimeError("MOVIE_UPDATE_CHANNEL is not configured")

    if poster_url and not LINK_PREVIEW and len(text) <= 1024:
        photo = await fetch_image(poster_url)
        if photo:
            try:
                msg = await bot.send_photo(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    photo=photo,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
                return msg, True
            except FloodWait:
                raise
            except Exception as e:
                logger.warning("Photo post failed (%s) — falling back to text", e)

    body, preview = _text_body(text, poster_url)
    msg = await bot.send_message(
        chat_id=MOVIE_UPDATE_CHANNEL,
        text=body,
        reply_markup=reply_markup,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=not preview,
        invert_media=bool(preview and ABOVE_PREVIEW),
    )
    return msg, False


async def send_movie_update(bot, base_name):
    """Send movie update to MOVIE_UPDATE_CHANNEL."""
    max_retries = 3

    for attempt in range(max_retries):
        try:
            movie_doc = await movie_update_db.get_movie_doc(base_name)
            if not movie_doc:
                return None

            text = generate_movie_message(movie_doc, base_name)
            msg, is_photo = await _post_notification(
                bot, text, _build_markup(movie_doc), movie_doc.get("poster_url")
            )
            await movie_update_db.update_message_id(base_name, msg.id, is_photo)
            return msg

        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
        except Exception:
            logger.exception("Failed to send movie update for %r", base_name)
            break
    return None


async def update_movie_message(bot, base_name):
    """Update existing movie message with new file info."""
    try:
        movie_doc = await movie_update_db.get_movie_doc(base_name)
        if not movie_doc:
            return

        text = generate_movie_message(movie_doc, base_name)
        reply_markup = _build_markup(movie_doc)
        message_id = movie_doc.get("message_id")
        is_photo = movie_doc.get("is_photo", False)

        if not message_id:
            await send_movie_update(bot, base_name)
            return

        # The first post went out without a poster (lookup missed). Try again
        # now; if found, repost so the notification gets its image.
        repost = False
        if not movie_doc.get("poster_url"):
            poster_url = await resolve_poster_url(movie_doc.get("display_name") or base_name, movie_doc.get("year"))
            if poster_url:
                await movie_update_db.update_movie_doc(base_name, {"$set": {"poster_url": poster_url}})
                repost = True

        for attempt in range(0 if repost else 2):
            try:
                if is_photo:
                    await bot.edit_message_caption(
                        chat_id=MOVIE_UPDATE_CHANNEL,
                        message_id=message_id,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=enums.ParseMode.HTML
                    )
                else:
                    body, preview = _text_body(text, movie_doc.get("poster_url"))
                    await bot.edit_message_text(
                        chat_id=MOVIE_UPDATE_CHANNEL,
                        message_id=message_id,
                        text=body,
                        reply_markup=reply_markup,
                        parse_mode=enums.ParseMode.HTML,
                        invert_media=bool(preview and ABOVE_PREVIEW),
                        disable_web_page_preview=not preview,
                    )
                return
            except MessageNotModified:
                return          # nothing changed — keep the existing post
            except FloodWait as e:
                await asyncio.sleep(e.value + 2)
            except Exception as e:
                logger.warning("Edit failed for %r (%s) — reposting", base_name, e)
                break
        else:
            if not repost:
                return          # still rate-limited; the next file will retry

        try:
            await bot.delete_messages(chat_id=MOVIE_UPDATE_CHANNEL, message_ids=message_id)
        except Exception:
            pass
        await movie_update_db.update_message_id(base_name, None, False)
        await send_movie_update(bot, base_name)
    except Exception:
        logger.exception("Failed to update movie message for %r", base_name)


def schedule_update(bot, base_name, delay=5):
    """Debounce: a burst of uploads for one title → ONE edit, `delay`s after the last."""
    old = pending_updates.get(base_name)
    if old and not old.done():
        old.cancel()
    pending_updates[base_name] = asyncio.create_task(_delayed_update(bot, base_name, delay))


async def _delayed_update(bot, base_name, delay):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    # From here on a newer schedule_update() must not cancel this edit midway.
    if pending_updates.get(base_name) is asyncio.current_task():
        pending_updates.pop(base_name, None)
    lock = locks.setdefault(base_name, asyncio.Lock())
    async with lock:
        await update_movie_message(bot, base_name)


# ══════════════════════════════════════════════════════════════════════════════
# Auto-fetch pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def process_and_send_update(bot, filename, caption, source_chat,
                                  message_link=None, file_unique_id=None):
    """Process file and send movie update notification."""
    try:
        media_info = extract_media_info(filename, caption)
        base_name = media_info["base_name"]
        processed = media_info["processed"]

        if not base_name:
            logger.warning("Movie update skipped — no title in %r / caption", filename)
            return

        if base_name not in locks:
            locks[base_name] = asyncio.Lock()

        async with locks[base_name]:
            await _process_with_lock(bot, filename, caption, media_info, base_name, processed,
                                     source_chat, message_link, file_unique_id)
    except Exception as e:
        logger.exception("Processing failed: %s", e)


def _is_duplicate(movie_doc, file_data) -> bool:
    for f in movie_doc.get("files", []):
        if f.get("filename") == file_data["filename"]:
            return True
        if file_data.get("file_unique_id") and f.get("file_unique_id") == file_data["file_unique_id"]:
            return True
    return False


async def _process_with_lock(bot, filename, caption, media_info, base_name, processed,
                             source_chat, message_link=None, file_unique_id=None):
    """Process file with lock to prevent concurrent updates."""
    movie_doc = await movie_update_db.get_movie_doc(base_name)

    # Source link: public channel → its @username link; private channel →
    # link to the message itself (t.me/c/<id> alone is not a valid button URL).
    if source_chat.username:
        channel_link = f"https://t.me/{source_chat.username}"
    else:
        channel_link = message_link or ""

    file_data = {
        "filename": filename or media_info["display_name"],
        "file_unique_id": file_unique_id,
        "processed": processed,
        "quality": media_info["quality"],
        "resolution": media_info["resolution"],
        "language": media_info["language"],
        "ott_platform": media_info["ott_platform"],
        "timestamp": datetime.now(),
        "tag": media_info["tag"],
        "season": media_info["season"],
        "episode": media_info["episode"],
        "source_channel": channel_link,
        "source_chat_id": source_chat.id,
    }

    if movie_doc:
        # Movie doc exists, add file if not duplicate
        if _is_duplicate(movie_doc, file_data):
            return
        if await movie_update_db.add_file_to_movie(base_name, file_data):
            schedule_update(bot, base_name)
        return

    # Fetch movie metadata
    details = {}
    used_tmdb = False
    title = media_info["display_name"]

    if TMDB_POSTER:
        tmdb_result = await get_movie_detailsx(
            title, year=media_info.get("year"), prefer_tv=bool(media_info["season"])
        )
        if tmdb_result and not tmdb_result.get("error"):
            details = tmdb_result
            used_tmdb = True
        else:
            details = await get_movie_details(title, file=filename) or {}
    else:
        details = await get_movie_details(title, file=filename) or {}

    if not details:
        logger.warning("All metadata sources failed for '%s' — sending without info", base_name)

    # Process genres
    raw_genres = details.get("genres", "") or ""
    if isinstance(raw_genres, list):
        genres = ", ".join(str(g) for g in raw_genres if g) or "N/A"
    elif isinstance(raw_genres, str) and raw_genres and raw_genres != "N/A":
        genres = ", ".join(g.strip() for g in raw_genres.split(",") if g.strip()) or "N/A"
    else:
        genres = "N/A"

    poster_url = await resolve_poster_url(title, media_info.get("year"), details, used_tmdb)

    info_url = details.get("imdb_url") or details.get("tmdb_url") or ""

    movie_doc = {
        "_id": base_name,
        "display_name": media_info["display_name"],
        "files": [file_data],
        "poster_url": poster_url,
        "genres": genres,
        "rating": details.get("rating") or "N/A",
        "imdb_url": info_url,
        "year": media_info["year"] or details.get("year"),
        "tag": media_info["tag"],
        "ott_platform": media_info["ott_platform"],
        "message_id": None,
        "is_photo": False
    }

    if await movie_update_db.insert_movie_doc(movie_doc):
        await send_movie_update(bot, base_name)
        return

    # Insert failed (e.g. another instance created the doc first) — treat as existing.
    existing = await movie_update_db.get_movie_doc(base_name)
    if existing and not _is_duplicate(existing, file_data):
        if await movie_update_db.add_file_to_movie(base_name, file_data):
            schedule_update(bot, base_name)


async def _active_fetch_channels() -> list:
    """Fetch channels from the settings DB. The settings document is seeded
    from the FETCH_MOVIE_UPDATE env var on first boot, so the env var is only
    a fallback if the DB can't be read — otherwise removing the last channel
    in /settings would silently bring the env channel back."""
    from database.settings_db import get_settings
    try:
        settings = await get_settings()
        return list(settings.get("fetch_movie_update_channels") or [])
    except Exception:
        logger.warning("Couldn't load fetch channels from settings, using env var", exc_info=True)
        from config import FETCH_MOVIE_UPDATE
        return list(FETCH_MOVIE_UPDATE or [])


# Automatic movie update fetcher.
# NOTE: group=1 — index.py's auto-indexer lives in group 0, and Pyrogram runs
# only the first matching handler per group. With both in group 0, a channel
# that is both an index channel and a fetch channel would only ever get
# indexed (or only get a movie update), never both.
@Client.on_message(media_filter, group=1)
async def movie_update_fetcher(bot, message):
    """Automatically process files uploaded to fetch channels."""
    if message.chat.id not in await _active_fetch_channels():
        return
    media = next(
        (getattr(message, ft) for ft in ("document", "video", "audio")
         if getattr(message, ft, None)),
        None
    )
    if not media:
        return

    try:
        if await movie_update_db.movie_update_status(bot.me.id):
            await process_and_send_update(
                bot,
                getattr(media, "file_name", None) or "",
                message.caption or "",
                source_chat=message.chat,
                message_link=message.link,
                file_unique_id=getattr(media, "file_unique_id", None),
            )
    except Exception:
        logger.exception("Movie update fetch failed")


# ══════════════════════════════════════════════════════════════════════════════
# Manual /m command
# ══════════════════════════════════════════════════════════════════════════════

_M_SEASON_RE = re.compile(r'\b(?:s|season\s*)0*(\d{1,2})$', re.IGNORECASE)


def _is_plausible_year(tok: str) -> bool:
    # 2049 (Blade Runner) etc. is a title, not a year: cap at next year.
    return bool(_YEAR_TOKEN_RE.fullmatch(tok)) and int(tok) <= datetime.now().year + 1


def _parse_m_query(raw: str):
    """Parse the argument of /m command → (title, year, season)."""
    text = raw.strip()
    season = None
    year = None

    m = _M_SEASON_RE.search(text)
    if m and text[:m.start()].strip():
        season = int(m.group(1))
        text = text[:m.start()].strip()

    tokens = text.split()
    if len(tokens) > 1:
        for idx in range(len(tokens) - 1, -1, -1):
            if _is_plausible_year(tokens[idx]):
                year = tokens.pop(idx)
                break
    text = " ".join(tokens)

    if season is None:
        m = _M_SEASON_RE.search(text)
        if m and text[:m.start()].strip():
            season = int(m.group(1))
            text = text[:m.start()].strip()

    return text.strip(), year, season


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9]+", _plain(text).lower().replace("'", ""))


def is_title_match(search_title: str, file_text: str) -> bool:
    """Check if file text contains all words from search title. Dots/underscores
    in file names and HTML tags in stored captions don't break the match."""
    search_words = set(_words(search_title))
    return bool(search_words) and search_words.issubset(set(_words(file_text)))


async def _build_manual_update_doc(title: str, year: str, season: int):
    """Build movie document from database search results."""
    search_term = f"{title} s{season:02d}" if season else title

    # Search DB — search_files() returns a plain list of file dicts
    files = await search_files(search_term)
    if not files and year:
        files = await search_files(f"{title} {year}")
    if not files:
        files = await search_files(title)

    # Filter for exact title matches
    files = [f for f in files if is_title_match(title, f"{f.get('file_name', '')} {f.get('caption', '') or ''}")]

    # Parse every file once with the same parser the auto-fetch uses
    parsed = [(f, extract_media_info(f.get("file_name", ""), _plain(f.get("caption", "") or ""))) for f in files]

    if season:
        parsed = [(f, i) for f, i in parsed if i["season"] == season]
    if year:
        same_year = [(f, i) for f, i in parsed if i["year"] == year]
        if same_year:
            parsed = same_year

    files = [f for f, _ in parsed]
    total = len(files)

    all_qualities = set()
    all_resolutions = set()
    all_languages = set()
    all_ott_platforms = set()
    all_tags = set()
    episodes_by_season = defaultdict(set)

    for _, info in parsed:
        if info["quality"] != "N/A":
            all_qualities.update(x.strip() for x in info["quality"].split(",") if x.strip())
        if info["resolution"] != "N/A":
            all_resolutions.update(x.strip() for x in info["resolution"].split(",") if x.strip())
        if info["language"] != "N/A":
            all_languages.update(x.strip() for x in info["language"].split(",") if x.strip())
        if info["ott_platform"] != "N/A":
            all_ott_platforms.update(p.strip() for p in info["ott_platform"].split("|") if p.strip())

        if info["season"] is not None and info["episode"] is not None:
            all_tags.add("#SERIES")
            episodes_by_season[str(info["season"])].add(str(info["episode"]))
        else:
            all_tags.add("#MOVIE")

    primary_tag = "#SERIES" if "#SERIES" in all_tags else "#MOVIE"
    epi_block = _format_episodes(episodes_by_season)

    return {
        "_id": title,
        "genres": "N/A",
        "rating": "N/A",
        "poster_url": None,
        "imdb_url": "",
        "tag": primary_tag,
        "ott_platform": " | ".join(sorted(all_ott_platforms)) or "N/A",
        "message_id": None,
        "is_photo": False,
        "_qualities": sorted(all_qualities),
        "_resolutions": sorted(all_resolutions, key=_res_sort_key),
        "_languages": sorted(all_languages),
        "_epi_block": epi_block,
        "_total_files": total,
    }, files


@Client.on_message(filters.command("m") & filters.user(ADMINS))
async def manual_movie_update(bot, message):
    """Manual movie update command for admins."""
    try:
        raw_arg = message.text.split(None, 1)[1].strip()
    except IndexError:
        return await message.reply_text(
            "<b>⚠️ Usage:</b>\n"
            "<code>/m pushpa 2</code>\n"
            "<code>/m suits s02</code>\n"
            "<code>/m ironman 2003</code>\n"
            "<code>/m dark knight</code>",
            parse_mode=enums.ParseMode.HTML
        )

    title, year, season = _parse_m_query(raw_arg)
    if not title:
        return await message.reply_text("<b>❌ Could not parse a title from your input.</b>")

    status_msg = await message.reply_text("<b>⏳ Fetching metadata and searching database…</b>")

    try:
        # Fetch metadata
        details = {}
        used_tmdb = False

        if TMDB_POSTER:
            tmdb_result = await get_movie_detailsx(title, year=year, prefer_tv=bool(season))
            if tmdb_result and not tmdb_result.get("error"):
                details = tmdb_result
                used_tmdb = True
            else:
                details = await get_movie_details(title, file=None) or {}
        else:
            details = await get_movie_details(title, file=None) or {}

        # Build display title
        meta_title = details.get("title") or ""
        if meta_title:
            display_title = meta_title
            if year:
                display_title += f" ({year})"
            elif details.get("year"):
                display_title += f" ({details['year']})"
            if season:
                display_title += f" Season {season}"
        else:
            display_title = title.title()
            if season:
                display_title += f" Season {season}"
            if year:
                display_title += f" ({year})"

        # Process genres and rating
        raw_genres = details.get("genres", "") or ""
        if isinstance(raw_genres, list):
            genres = ", ".join(str(g) for g in raw_genres if g) or "N/A"
        elif isinstance(raw_genres, str) and raw_genres and raw_genres != "N/A":
            genres = ", ".join(g.strip() for g in raw_genres.split(",") if g.strip()) or "N/A"
        else:
            genres = "N/A"

        rating_raw = details.get("rating", "N/A")
        try:
            rating_display = f"{float(rating_raw):.1f}" if rating_raw and rating_raw != "N/A" else "N/A"
        except (ValueError, TypeError):
            rating_display = str(rating_raw) if rating_raw else "N/A"

        poster_url = await resolve_poster_url(details.get("title") or title, year, details, used_tmdb)

        # Search DB for files
        pseudo_doc, db_files = await _build_manual_update_doc(title, year, season)
        total_files = pseudo_doc["_total_files"]
        epi_block = pseudo_doc["_epi_block"]

        # Determine tag
        primary_tag = pseudo_doc["tag"]
        if primary_tag == "#MOVIE" and details.get("kind") == "tv":
            primary_tag = "#SERIES"
        if season:
            primary_tag = "#SERIES"

        e = html.escape
        text = MANUAL_UPDATE_NOTIFY_TXT.format(
            tag=primary_tag,
            filename=e(display_title),
            genres=e(genres),
            ott=e(pseudo_doc.get("ott_platform") or "N/A"),
            quality=e(", ".join(pseudo_doc["_qualities"]) or "N/A"),
            resolution=e(", ".join(pseudo_doc["_resolutions"]) or "N/A"),
            language=e(", ".join(pseudo_doc["_languages"]) or "N/A"),
            rating=e(rating_display),
            episodes=epi_block,
        )

        # Build buttons with bot query search link. A /start payload is capped
        # at 64 chars ("getfile-" is 8), so cut on a word boundary.
        raw_search = title
        if season:
            raw_search = f"{raw_search} s{season:02d}"
        words = re.sub(r"[^A-Za-z0-9_\s]", " ", raw_search).split()
        search_query = ""
        for w in words:
            candidate = f"{search_query}-{w}" if search_query else w
            if len(candidate) > 56:
                break
            search_query = candidate

        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔍 ɢᴇᴛ ꜰɪʟᴇs",
                url=f"https://t.me/{temp.U_NAME}?start=getfile-{search_query}"
            )],
            [InlineKeyboardButton(
                "♨️ Viral Stuff ♨️",
                url="https://t.me/Reload_adultbot"
            )],
        ])

        # Send to MOVIE_UPDATE_CHANNEL
        await _post_notification(bot, text, reply_markup, poster_url)

        # Confirm to admin
        files_note = f"({total_files} files in DB)" if total_files else "(no files found in DB yet)"
        await status_msg.edit_text(
            f"<b>✅ Update posted!</b>\n"
            f"<b>Title:</b> {e(display_title)}\n"
            f"<b>DB:</b> {files_note}",
            parse_mode=enums.ParseMode.HTML
        )

    except Exception as exc:
        logger.exception("Manual movie update failed: %s", exc)
        await status_msg.edit_text(f"<b>❌ Failed:</b> <code>{html.escape(str(exc))}</code>", parse_mode=enums.ParseMode.HTML)

# Movie update notifications are managed from the unified /settings panel
# (Settings -> 🎬 Movie Updates): the on/off toggle lives in the global
# settings document and fetch channels are admin-managed there too. There is
# deliberately no separate /movie_update command any more.
