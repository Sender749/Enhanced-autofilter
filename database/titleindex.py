"""
Title index — an in-memory list of EVERY distinct title in the database, used
for two things:

  1. Phrase search. "bahubali" finds every title that is, starts with, or
     contains those words — spelling-proof, so Bahubali = Baahubali and
     "Baahubali The Epic" = "Bahubali: The Epic" are ONE title — ranked
     exact -> starts-with -> contains -> words-in-order, each with its file count
     and years, ready to be shown as buttons.
  2. The typo fixer's title list (spellcheck.py), which used to look at only the
     first 8,000 FILES — about 11% of a 72,000-file database.

Two spelling levels:
  gentle  keeps the vowels:   aa=a  ee=i  oo=u  ph=f  w=v  q=k  double letters
  loose   keeps only the consonants (Baahubali = Bahubli = bhbl). Used only
          when the gentle level finds nothing, and never for words of 4 letters
          or fewer — otherwise "don" would also match Doona, Dino Dan, Den.

The index is rebuilt in the background every REFRESH_SECONDS (10 minutes), so
new files show up within that time; searches never wait for a rebuild after the
first one. ~12,000 titles use about 2 MB.
"""
import asyncio
import logging
import re
import time
from collections import Counter, defaultdict

from database.filters_db import (
    clean_title, display_name, files, parse_query, title_key_variants, search_by_title_keys,
    _season_of, _episode_of, _episode_range_of,
)
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 600
SCAN_BATCH = 5000
MAX_DOCS = 500_000
MAX_TITLES_RETURNED = 60

_YEAR_RE = re.compile(r"(?<![\w])(19[5-9]\d|20[0-3]\d)(?![\w])")
_HTML_RE = re.compile(r"<[^>]+>")
_APOS_RE = re.compile(r"['\u2019\u2018`\u00b4]")
_WORD_RE = re.compile(r"[^\W_]+")


# ── spelling keys ───────────────────────────────────────────────────────────

def tokens(text: str) -> list:
    return _WORD_RE.findall(_APOS_RE.sub("", (text or "").lower()))


def gentle(word: str) -> str:
    w = word.lower().replace("ph", "f").replace("w", "v").replace("q", "k")
    w = w.replace("ee", "i").replace("oo", "u")
    return re.sub(r"(.)\1+", r"\1", w)


def loose(word: str) -> str:
    w = re.sub(r"(.)\1+", r"\1", word.lower())
    w = w.replace("ph", "f").replace("w", "v").replace("q", "k").replace("x", "ks")
    w = re.sub(r"([bcdfgjkpstzv])h", r"\1", w).replace("c", "k").replace("z", "j")
    return w[:1] + re.sub(r"[aeiou]", "", w[1:])


_CONTRACTION_RE = re.compile(r"\b([A-Za-z]*n) (t)\b", re.IGNORECASE)             # Don t -> Don't
_POSSESSIVE_RE = re.compile(r"\b([A-Za-z]{3,}) (s)\b(?= [A-Za-z])", re.IGNORECASE)  # Shadow S Edge -> Shadow's Edge


def pretty(title: str) -> str:
    """clean_title() turns every apostrophe into a space ("Don t Move"); put the
    obvious ones back so a button reads "Don't Move"."""
    t = _CONTRACTION_RE.sub(lambda m: f"{m.group(1)}'{m.group(2).lower()}", title)
    return _POSSESSIVE_RE.sub(lambda m: f"{m.group(1)}'{m.group(2).lower()}", t)


# ── the index ───────────────────────────────────────────────────────────────

class Group:
    """One title, however it is spelled in the captions."""
    __slots__ = ("gt", "st", "names", "members", "n", "years", "seasons", "series")

    def __init__(self, gt, st):
        self.gt, self.st = gt, st
        self.names: Counter = Counter()
        self.members: set = set()
        self.n = 0
        self.years: Counter = Counter()
        self.seasons: Counter = Counter()   # season number -> files (series only)
        self.series = 0                     # files that are episodes / seasons

    @property
    def title(self) -> str:
        return pretty(self.names.most_common(1)[0][0])


_INDEX: dict | None = None
_BUILT_AT = 0.0
_BUILD_LOCK = asyncio.Lock()
_refresh_task: asyncio.Task | None = None


def _doc_year(doc: dict) -> str | None:
    m = _YEAR_RE.search(_HTML_RE.sub(" ", display_name(doc)))
    return m.group(1) if m else None


def doc_year(doc: dict) -> str | None:
    return _doc_year(doc)


def _scan_batch(batch: list, titles: dict):
    for doc in batch:
        title = clean_title(display_name(doc))
        key = title.lower().strip()
        if not key:
            continue
        t = titles.get(key)
        if t is None:
            t = titles[key] = {"n": 0, "names": Counter(), "years": Counter(),
                               "seasons": Counter(), "series": 0}
        t["n"] += 1
        t["names"][title] += 1
        y = _doc_year(doc)
        if y:
            t["years"][y] += 1
        text = display_name(doc)
        season = _season_of(text)
        if season or _episode_of(text) or _episode_range_of(text):
            t["series"] += 1
            if season:
                t["seasons"][season] += 1


def _assemble(titles: dict) -> dict:
    groups, by_gt = [], {}
    for key, t in titles.items():
        toks = tokens(key)
        if not toks:
            continue
        gt = tuple(gentle(x) for x in toks)
        g = by_gt.get(gt)
        if g is None:
            g = by_gt[gt] = Group(gt, tuple(loose(x) for x in toks))
            groups.append(g)
        g.members.add(key)
        g.n += t["n"]
        g.names.update(t["names"])
        g.years.update(t["years"])
        g.seasons.update(t.get("seasons", ()))
        g.series += t.get("series", 0)

    inv_g, inv_s = defaultdict(list), defaultdict(list)
    first_g, first_s = defaultdict(list), defaultdict(list)
    for i, g in enumerate(groups):
        for tok in set(g.gt):
            inv_g[tok].append(i)
        for tok in set(g.st):
            inv_s[tok].append(i)
        first_g[g.gt[0]].append(i)
        first_s[g.st[0]].append(i)

    cache = sorted(
        ((key, max(t["names"].items(), key=lambda kv: kv[1])[0]) for key, t in titles.items()),
        key=lambda kv: -titles[kv[0]]["n"],
    )
    return {"groups": groups, "inv_g": inv_g, "inv_s": inv_s, "first_g": first_g, "first_s": first_s,
            "cache": cache, "titles": len(titles)}


async def _build():
    global _INDEX, _BUILT_AT
    async with _BUILD_LOCK:
        started = time.monotonic()
        titles: dict = {}
        batch, scanned = [], 0
        async for doc in files.find({}, {"file_name": 1, "caption": 1}).limit(MAX_DOCS):
            batch.append(doc)
            scanned += 1
            if len(batch) >= SCAN_BATCH:
                await asyncio.to_thread(_scan_batch, batch, titles)
                batch = []
        if batch:
            await asyncio.to_thread(_scan_batch, batch, titles)
        index = await asyncio.to_thread(_assemble, titles)
        _INDEX, _BUILT_AT = index, time.monotonic()
        logger.info("Title index built: %s files -> %s titles in %.1fs", scanned, index["titles"],
                    time.monotonic() - started)


async def ensure_index():
    """Build on first use; afterwards refresh in the background (callers keep
    using the current index meanwhile)."""
    global _refresh_task
    if _INDEX is None:
        await _build()
    elif time.monotonic() - _BUILT_AT > REFRESH_SECONDS and (_refresh_task is None or _refresh_task.done()):
        _refresh_task = asyncio.create_task(_build())


async def title_cache() -> list:
    """[(lower-case clean title, display title)] for ALL titles — the typo fixer's list."""
    await ensure_index()
    return _INDEX["cache"] if _INDEX else []


# ── phrase matching ─────────────────────────────────────────────────────────

def _tier(gt: tuple, q: tuple, jq: str):
    """0 exact · 1 starts with · 2 contains · 3 all words in order · None."""
    n = len(q)
    if gt == q or "".join(gt) == jq:
        return 0
    if gt[:n] == q:
        return 1
    acc = ""
    for k, tok in enumerate(gt):  # "spiderman" == "spider man ..." at a word boundary
        acc += tok
        if acc == jq:
            return 1
        if len(acc) >= len(jq):
            break
    for i in range(1, len(gt)):
        if gt[i:i + n] == q:
            return 2
        acc = ""
        for tok in gt[i:]:
            acc += tok
            if acc == jq:
                return 2
            if len(acc) >= len(jq):
                break
    # words in order with at most 2 other words between/around them
    pos, extra = -1, 0
    for word in q:
        try:
            nxt = gt.index(word, pos + 1)
        except ValueError:
            return None
        extra += nxt - pos - 1 if pos >= 0 else 0
        pos = nxt
    return 3 if extra <= 2 else None


def _match(index: dict, q: tuple, level: str) -> list:
    suffix, attr = ("g", "gt") if level == "gentle" else ("s", "st")
    inv, first = index["inv_" + suffix], index["first_" + suffix]
    jq = "".join(q)
    cand = None
    for tok in q:
        ids = set(inv.get(tok, ()))
        cand = ids if cand is None else cand & ids
        if not cand:
            break
    cand = set(cand or ())
    for p in range(2, len(jq) + 1):  # collapsed spacing: "ironman" vs "iron man"
        cand.update(first.get(jq[:p], ()))
    out = []
    groups = index["groups"]
    for i in cand:
        t = _tier(getattr(groups[i], attr), q, jq)
        if t is not None:
            out.append((t, groups[i]))
    if any(t <= 2 for t, _ in out):
        out = [(t, g) for t, g in out if t <= 2]
    return sorted(out, key=lambda tg: (tg[0], -tg[1].n))


async def title_groups(query: str) -> list:
    """[(tier, Group)] best first; [] when nothing matches (or the query is so
    short / broad that listing titles would be noise)."""
    await ensure_index()
    if not _INDEX:
        return []
    title = clean_title(parse_query(query)[0]) or query
    toks = tokens(title)
    if not toks or len("".join(toks)) < 3:
        return []
    q = tuple(gentle(t) for t in toks)
    found = _match(_INDEX, q, "gentle")
    # The loose (consonants-only) level is for longer queries: a single word of
    # 5+ letters, or a phrase of 8+ letters ("vyah kartare da" = "Viyaah Kartaare Da").
    joined = len("".join(toks))
    if not found and ((len(toks) == 1 and joined >= 5) or (len(toks) > 1 and joined >= 8)):
        found = _match(_INDEX, tuple(loose(t) for t in toks), "loose")
    # "contains" matches only for specific queries — a short single word would
    # otherwise list half the database.
    if len(toks) == 1 and len(toks[0]) < 5:
        found = [(t, g) for t, g in found if t <= 1]
    if len(found) > 200:
        return []
    return found[:MAX_TITLES_RETURNED]


# ── buttons ─────────────────────────────────────────────────────────────────

def season_label(seasons) -> str:
    """[1, 2, 3, 4, 5] -> "Season 1-5" · [2] -> "Season 2" · [1, 3, 4] -> "Seasons 1, 3-4"."""
    nums = sorted({int(x) for x in seasons if x})
    if not nums:
        return ""
    runs, start, prev = [], nums[0], nums[0]
    for x in nums[1:]:
        if x == prev + 1:
            prev = x
            continue
        runs.append((start, prev))
        start = prev = x
    runs.append((start, prev))
    text = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
    if len(nums) == 1:
        return f"Season {nums[0]}"
    return f"Season {text}" if len(runs) == 1 else f"Seasons {text}"


def group_buttons(tier: int, group: Group) -> list:
    """The button(s) for one title. A title whose files fall into two or more
    years with 2+ files each (Gadar 2001 / Gadar 2023, remakes) becomes one
    button per year; files with an odd or no year stay with the biggest year.
    A SERIES gets one button that shows its seasons instead of a year ("Season 1-5")."""
    title = group.title
    members = sorted(group.members)[:40]
    if group.series and group.series * 2 >= group.n:
        label = season_label(group.seasons) or "Series"
        return [{"title": title, "members": members, "years": None, "split": [], "other": True,
                 "n": group.n, "year_label": "", "season_label": label, "seasons": sorted(group.seasons),
                 "tier": tier}]
    split = sorted(y for y, c in group.years.items() if c >= 2)
    if len(split) >= 2:
        main = max(split, key=lambda y: group.years[y])
        leftover = group.n - sum(group.years[y] for y in split)
        return [{
            "title": title, "members": members, "years": [y], "split": split, "other": y == main,
            "n": group.years[y] + (leftover if y == main else 0), "year_label": y, "tier": tier,
        } for y in split]
    ys = sorted(group.years)
    year_label = ys[0] if len(ys) == 1 else (f"{ys[0]}–{ys[-1]}" if ys else "")
    return [{"title": title, "members": members, "years": None, "split": [], "other": True,
             "n": group.n, "year_label": year_label, "tier": tier}]


def button_label(btn: dict) -> str:
    extra = btn.get("season_label") or btn.get("year_label")
    return f"{btn['title']}{f' ({extra})' if extra else ''} · {btn['n']} files"


# ── near-duplicate titles ───────────────────────────────────────────────────
# "The Last Kingdom" / "Last Kingdom", "Mr. Bean's Holiday" / "Mr Beans Holiday" written two
# ways in the captions: same year (movies) and an almost identical title -> ONE button.
# Numbers never merge ("Weak Hero Class 1" / "2").

_SIMILAR_PERCENT = 90
_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _sim_form(title: str):
    toks = tokens(title)
    if len(toks) > 1 and toks[0] in ("the", "a", "an"):
        toks = toks[1:]
    return toks


def _almost_same(t1: str, t2: str) -> bool:
    a, b = _sim_form(t1), _sim_form(t2)
    if not a or not b:
        return False
    num = lambda toks: sorted(w for w in toks if w.isdigit() or w in _ROMAN)
    if num(a) != num(b):
        return False
    return fuzz.ratio(" ".join(a), " ".join(b)) >= _SIMILAR_PERCENT


def _merge_into(a: dict, b: dict) -> None:
    """Fold button b into button a (a keeps the title of the bigger one)."""
    if b["n"] > a["n"]:
        a["title"] = b["title"]
    a["members"] = sorted(set(a["members"]) | set(b["members"]))[:40]
    a["n"] += b["n"]
    a["tier"] = min(a["tier"], b["tier"])
    if a.get("season_label") is not None and b.get("season_label") is not None:
        a["seasons"] = sorted(set(a.get("seasons", [])) | set(b.get("seasons", [])))
        a["season_label"] = season_label(a["seasons"]) or "Series"
    elif a["years"] is not None:
        a["split"] = sorted(set(a["split"]) | set(b["split"]))
        a["other"] = a["other"] or b["other"]


def merge_similar(buttons: list) -> list:
    out = []
    for b in buttons:
        target = None
        for a in out:
            same_kind = (a.get("season_label") is not None) == (b.get("season_label") is not None)
            if not same_kind:
                continue
            if a.get("season_label") is None:      # movies: same year (or both without one)
                if a["year_label"] != b["year_label"] or (a["years"] is None) != (b["years"] is None):
                    continue
            if _almost_same(a["title"], b["title"]):
                target = a
                break
        if target is None:
            out.append(dict(b))
        else:
            _merge_into(target, b)
    return out


async def buttons_for(query: str) -> list:
    out = []
    for tier, group in await title_groups(query):
        out.extend(group_buttons(tier, group))
    return merge_similar(out)


async def files_for_button(btn: dict) -> list:
    keys = set()
    for member in btn["members"]:
        keys.update(title_key_variants(member))
    results = await search_by_title_keys(sorted(keys))
    if btn["years"] is None:
        return results
    wanted, split = set(btn["years"]), set(btn["split"])
    return [d for d in results
            if (_doc_year(d) in wanted) or (btn["other"] and _doc_year(d) not in split)]
