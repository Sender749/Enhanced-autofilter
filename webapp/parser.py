"""
Caption parser for the web app catalog.

Turns one file caption into: clean title, year, season, episode range
(ep_from..ep_to, equal for a single episode), pack type, quality, languages.
Written against a real export of ~66k captions from this bot's database
(HTML noise, combined ranges like S01E06-10 / S02 E10 E13 / Ep.01-08,
"Complete"/"COMBINED" packs, S01-09 meaning episodes 1-9, bare E108, ...).
Pure functions, no I/O.
"""
import html
import re

P_SE = re.compile(
    r"\bS(?:eason)?\s*0*(\d{1,2})\s*[\[\(\-]?\s*E(?:p(?:isodes?)?)?\.?\s*0*(\d{1,4})"
    r"(?:\s*(?:-|to|&|\+)\s*E?(?:p)?\.?\s*0*(\d{1,4})(?!\d)(?!p\b)|\s+E(?:p)?\.?\s*0*(\d{1,4})(?!\w))?", re.I)
P_SEASON_RANGE = re.compile(r"\bS0*(\d{1,2})\s*(?:-|to|&)\s*S0*(\d{1,2})\b", re.I)
P_S_DASH_EP = re.compile(r"\bS0*(\d{1,2})\s*-\s*0*(\d{1,3})\b(?!\s*-)", re.I)
P_SEASON = re.compile(r"\b(?:S|Season\s*)0*(\d{1,2})\b", re.I)
P_EP_RANGE = re.compile(r"\bE(?:p(?:isodes?)?)?\.?\s*0*(\d{1,4})\s*(?:-|to)\s*0*(\d{1,4})(?!\d)(?!p\b)", re.I)
P_EP_SINGLE = re.compile(r"\bEp(?:isode)?\.?\s*0*(\d{1,4})\b", re.I)
P_E_LOOSE = re.compile(r"\bE0*(\d{1,4})(?:\s*(?:-|to)\s*E?0*(\d{1,4})(?!\d)(?!p\b)|\s+E0*(\d{1,4})(?!\w))?(?![\w])", re.I)
P_COMPLETE = re.compile(r"\b(complete(?:d)?(?:\s+season)?|all\s+episodes?|full\s+season)\b", re.I)
P_COMBINED = re.compile(r"\bcombined(?:\s+episodes?)?\b", re.I)
P_BONUS = re.compile(r"\bbonus\b", re.I)
P_YEAR = re.compile(r"\b(19[2-9]\d|20[0-3]\d)\b")
P_Q = re.compile(r"\b(240|360|480|540|576|720|1080|1440|2160)p\b", re.I)
P_STOP = re.compile(
    r"[\(\[]|\b(19[2-9]\d|20[0-3]\d)\b|\b\d{3,4}p\b|\b(x26[45]|hevc|h\.?26[45]|web[- ]?dl|web[- ]?rip|"
    r"hd[- ]?rip|blu[- ]?ray|bdrip|hdts|hdtc|hdcam|dvdrip|nf|amzn|dual|multi|hindi|english|tamil|telugu|"
    r"korean|japanese|punjabi|chinese|bengali|marathi|malayalam|kannada|thai|turkish|spanish|german|french|"
    r"web series|uncut|unrated|esubs?|msubs?)\b", re.I)
LANGS = ["hindi","english","tamil","telugu","korean","japanese","punjabi","chinese","bengali","marathi",
         "malayalam","kannada","thai","turkish","spanish","german","french","urdu"]


def clean(raw):
    s = html.unescape(raw)
    s = re.sub(r"[\u3164\u200b-\u200f\u2060\ufeff]", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^\w\s\.\-\[\]\(\)&+:,'’!]", " ", s)
    s = s.replace("_", " ")
    if s.count(".") >= 3 and s.count(" ") < 3:
        s = s.replace(".", " ")
    s = re.sub(r"(?<=\w)\.(?=\])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(raw):
    t = clean(raw)
    r = dict(clean=t, season=None, ep_from=None, ep_to=None, pack=None, seasons=None,
             bonus=False, flags=[], span_strip=[])
    spans = []
    m = P_SE.search(t)
    if m:
        r["season"] = int(m.group(1))
        a = int(m.group(2))
        b = m.group(3) or m.group(4)
        b = int(b) if b else a
        end = m.end()
        spaced = bool(re.search(r"\s-\s", m.group(0)))
        if b < a or (spaced and b - a > 30):
            r["flags"].append("bad_range" if b < a else "hyphen_in_episode_title_treated_single")
            b = a
            end = m.end(2)
        elif b - a > 80:
            r["flags"].append("huge_range")
        r["ep_from"], r["ep_to"] = a, b
        spans.append((m.start(), end))
    else:
        sr = P_SEASON_RANGE.search(t)
        sd = P_S_DASH_EP.search(t) if not sr else None
        er = P_EP_RANGE.search(t)
        es = P_EP_SINGLE.search(t) if not er else None
        sm = P_SEASON.search(t)
        if sr:
            r["seasons"] = [int(sr.group(1)), int(sr.group(2))]
            r["pack"] = "multi_season"
            spans.append(sr.span())
        elif sd and not er:
            r["season"] = int(sd.group(1))
            r["ep_from"], r["ep_to"] = 1, int(sd.group(2))
            r["flags"].append("ambiguous_S_dash_N_as_episodes")
            spans.append(sd.span())
        else:
            if sm:
                r["season"] = int(sm.group(1))
                spans.append(sm.span())
            if er:
                a, b = int(er.group(1)), int(er.group(2))
                if b < a:
                    r["flags"].append("bad_range"); b = a
                r["ep_from"], r["ep_to"] = a, b
                spans.append(er.span())
            elif es:
                r["ep_from"] = r["ep_to"] = int(es.group(1))
                spans.append(es.span())
        if r["ep_from"] is None and (r["season"] is not None or re.search(r"web series", t, re.I)) and not P_COMPLETE.search(t):
            lm = P_E_LOOSE.search(t)
            if lm:
                a = int(lm.group(1)); b = lm.group(2) or lm.group(3); b = int(b) if b else a
                if b < a: b = a
                r["ep_from"], r["ep_to"] = a, b
                spans.append(lm.span())
                r["flags"].append("episode_found_loosely")
        if r["ep_from"] is None and r["pack"] is None:
            if P_COMPLETE.search(t):
                r["pack"] = "complete"
            elif P_COMBINED.search(t) and r["season"] is not None:
                r["pack"] = "complete"
                r["flags"].append("combined_no_range_assumed_complete")
            elif r["season"] is not None:
                r["pack"] = "season_only"
                r["flags"].append("season_only_assumed_pack")
            for pm in (P_COMPLETE.search(t), P_COMBINED.search(t)):
                if pm:
                    spans.append(pm.span())
        if r["ep_from"] is not None and r["season"] is None:
            r["flags"].append("episode_without_season")
    if P_BONUS.search(t):
        r["bonus"] = True
    if r["ep_from"] is not None and r["ep_from"] >= 100:
        r["flags"].append("absolute_numbering")

    # title
    tt = t
    for a, b in sorted(spans, reverse=True):
        tt = tt[:a] + " " + tt[b:]
    tt = re.sub(r"\bWeb Series\b", " ", tt, flags=re.I)
    tt = re.sub(r"\s+", " ", tt).strip(" -•:~.")
    m2 = P_STOP.search(tt)
    cut = tt
    if m2 and m2.start() > 0:
        cut = tt[:m2.start()]
    elif m2 and m2.start() == 0:
        rest = tt[m2.end():]
        m3 = P_STOP.search(rest)
        cut = tt
    title = re.sub(r"\s+", " ", cut).strip(" -•:~.[]()")
    title = re.sub(r"^(S\d+\s*)+", "", title).strip(" -•:~.")
    r["title"] = title
    ym = P_YEAR.search(t)
    r["year"] = ym.group(1) if ym else None
    qm = P_Q.search(t)
    r["quality"] = qm.group(1) + "p" if qm else None
    low = t.lower()
    r["langs"] = [l for l in LANGS if re.search(r"\b" + l + r"\b", low)]
    r["is_series"] = bool(r["season"] or r["ep_from"] or r["pack"] or re.search(r"web series", t, re.I) or r["seasons"])
    r["anime"] = bool(re.search(r"\banime\b", t, re.I))
    if not title:
        r["flags"].append("junk_or_empty_caption")
    elif len(title) < 2:
        r["flags"].append("very_short_title")
    if r["quality"] is None:
        r["flags"].append("no_quality")
    return r


def category(r):
    if not r["is_series"]:
        return "movie"
    if r["bonus"]:
        return "series_bonus_extra"
    if r["pack"] == "multi_season":
        return "series_multi_season_pack"
    if r["pack"] == "complete":
        return "series_complete_season_pack"
    if r["pack"] == "season_only":
        return "series_season_unlabelled_pack"
    if r["ep_from"] is None:
        return "series_other"
    if r["ep_from"] == r["ep_to"]:
        return "series_single_episode"
    return "series_combined_episodes"


def key(r):
    k = re.sub(r"[^a-z0-9]", "", re.sub(r"^the\s+", "", r["title"].lower()))
    return k if r["is_series"] else k + (r["year"] or "")
