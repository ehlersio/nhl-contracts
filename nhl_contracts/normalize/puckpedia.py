"""Regex extraction of contract facts from PuckPedia's public Bluesky posts (tier D).

PuckPedia posts every signing as a short, regular message::

    The #NHLBruins signed 24 y/o F Marat Khusnutdinov to a three year $3.95M cap hit contract extension

    Yr 1: $2.2M Salary, $1.5M Signing Bonus
    Yr 2: $3.8M Salary, $500K SB
    Yr 3: $3.85M Salary

    Will be UFA upon expiry
    Rep'd by Paul Theofanous
    https://puckpedia.com/signing/10842

The header line names the team (a fan hashtag), the player, term, cap hit and
contract type; the following lines give the year-by-year structure, the
two-way split (``NHL: / Minors: / Guaranteed:``), trade protection, expiry
status and the agent. The posts are hand-typed, so the parser tolerates
typos ("Cal Hit", "$85M Min", "Singing Bonus") and shorthand ("Yr 2: $900K/$107.5K/$85K"
reuses Yr 1's labels by position). It never fills a number that is not in the
text: amounts are kept as written and oddities are flagged.

Output: :class:`Claim`, :class:`SeasonClaim`, :class:`ClauseClaim` and
:class:`Source` rows from ``model.py``, all with ``confidence_tier`` "D".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from .model import Claim, ClauseClaim, SeasonClaim, Source, claim_id, make_key
from .text import NUMBER_WORDS, name_key, season_from_date, shift_season

TIER = "D"
PUBLISHER = "PuckPedia (public Bluesky post)"
KIND = "bluesky_puckpedia"
POST_URL = "https://bsky.app/profile/puckpedia.com/post/{rkey}"

# --- team hashtags -------------------------------------------------------------
# Built from every hashtag in a year of posts (tests/fixtures/bluesky). Keys are
# lowercase; typos seen in real posts are kept so they still map.
HASHTAG_TEAMS: dict[str, str] = {
    "flytogether": "ANA",                       # Ducks (Mintyukov, Washe, "ANA transfers")
    "nhlbruins": "BOS", "nhlbuins": "BOS",
    "sabres": "BUF", "sabrehood": "BUF", "letsgobuffalo": "BUF",
    "flames": "CGY",
    "canes": "CAR", "carolinaculture": "CAR", "soundthesiren": "CAR",   # 2026 Cup winners
    "blackhawks": "CHI",
    "goavsgo": "COL",
    "cbj": "CBJ",
    "texashockey": "DAL",
    "lgrw": "DET",
    "letsgooilers": "EDM", "letssgooilers": "EDM",
    "flapanthers": "FLA", "timetohunt": "FLA",
    "gokingsgo": "LAK", "lakings": "LAK",
    "mnwild": "MIN",
    "gohabsgo": "MTL",
    "smashville": "NSH", "preds": "NSH",
    "njdevils": "NJD",
    "isles": "NYI", "nyi": "NYI",
    "nyr": "NYR",
    "gosensgo": "OTT", "sens": "OTT",
    "letsgoflyers": "PHI", "flyers": "PHI", "nhlflyers": "PHI",
    "letsgopens": "PIT", "gopensgo": "PIT",
    "sjsharks": "SJS", "sjssharks": "SJS", "thefutureisteal": "SJS", "thefuturesteal": "SJS",
    "seakraken": "SEA",
    "stlblues": "STL",
    "gobolts": "TBL",
    "leafsforever": "TOR", "leafs": "TOR",
    "tusksup": "UTA", "tuskup": "UTA",
    "canucks": "VAN",
    "vegasborn": "VGK", "forgedingold": "VGK",
    "allcaps": "WSH", "allcap": "WSH",
    "gojetsgo": "WPG", "nhljets": "WPG",
}
NON_TEAM_TAGS = {"nhl"}
HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9]*)")

# --- vocabulary ----------------------------------------------------------------
AMT = re.compile(
    r"(?P<dollar>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>[KkMm])(?![A-Za-z])|\s?(?P<word>million)\b)?"
)
TERM = re.compile(
    r"\b(?P<t>one|two|three|four|five|six|seven|eight|[1-8])\s*-?\s*(?:years?|yrs?)\b"
    r"|\b(?P<x>[1-8])\s*x\s*\$"
    r"|\b(?P<e>[1-8])\s+ELC\b",
    re.I,
)
SIGN_VERB = re.compile(r"\b(?:re-?)?(?:sign(?:s|ed|ing)?|singed)\b", re.I)
CONTRACT_WORD = re.compile(r"\b(?:contracts?|deals?|extensions?|ext|ELC|entry[\s-]level|PTO)\b", re.I)
NOT_SIGNING = re.compile(r"cap space|offer sheet|opening roster|\bLTIR\b|\bbuyout", re.I)
TRADE_HEAD = re.compile(r"^\W*trade\b", re.I)
ELC = re.compile(r"\bELC\b|\bentry[\s-]level\b", re.I)
EXT = re.compile(r"\b(?:extension|ext|extended|extends)\b", re.I)
PTO = re.compile(r"\b(?:to|on)\s+an?\s+(?:PTO|professional tryout)\b", re.I)
ONE_WAY = re.compile(r"\b(?:1|one)[\s-]?way\b", re.I)
TWO_WAY = re.compile(r"\b(?:2|two)[\s-]?way\b", re.I)
CAP_AFTER = re.compile(r"(?:cap|cal)\s*hit(?P<aav>\s*(?:/|and|&)\s*AAV)?", re.I)
CAP_BEFORE = re.compile(r"(?:cap|cal)\s*hit(?P<aav>\s*(?:/|and|&)\s*AAV)?[ \t]*:?[ \t]*$", re.I)
START = re.compile(
    r"\b(?:start(?:s|ing)?(?:\s+in)?|for\s+(?:the\s+)?(?:remainder\s+of\s+the\s+)?)\s*(\d\d)[/-](\d\d)\b", re.I
)
NEXT_SEASON = re.compile(r"\bstart(?:s|ing)?\s+next\s+season\b", re.I)
REMAINDER = re.compile(r"\bremainder of the\b", re.I)
SEASON_LABEL = re.compile(r"^(\d\d)-(\d\d):\s*")
YR = re.compile(
    r"(?<![A-Za-z])(?:yrs?|years?)'?s?\s*(?P<a>[1-8])(?!\d|[.,]\d|\s*[KkMm]\b)"
    r"(?:\s*(?:-|–|&|/|and|to|through)\s*(?P<b>[1-8])(?!\d|[.,]\d|\s*[KkMm]\b))?\s*:?",
    re.I,
)
ORDINAL_YR = re.compile(r"\b(?P<a>[1-8])(?:st|nd|rd|th)\s+(?:year|yr|season)\b", re.I)
THROUGH_YR = re.compile(r"\bthrough\s+(?:yr|year)\s*(?P<b>[1-8])\b", re.I)
WHOLE_TERM = re.compile(r"\bboth\s+(?:years|seasons?)\b|\ball\s+(?:\d\s+)?(?:years|seasons)\b|\bthrough\s*out\b",
                        re.I)
CLAUSE_WORD = re.compile(r"\b(?:M-NTC|NTC|NMC|MNC|no[\s-]?trade|no[\s-]?move|trade\s+list|trade\s+protection)\b", re.I)
CLAUSE = re.compile(
    r"(?:(?P<full>full)\s+)?(?:(?P<n>\d{1,2})\s*-?\s*team\s+)?"
    r"(?P<kind>M-NTC|NTC|NMC|MNC|no[\s-]move(?:\s+clause)?|no[\s-]trade(?!\s+protection)(?:\s+(?:clauses?|list))?"
    r"|trade\s+list)\b",
    re.I,
)
NO_PROTECTION = re.compile(r"\b(?:no|not\s+(?:include|includes|contain)\s+(?:any\s+)?)(?:bonus\s+or\s+)?"
                           r"trade\s+protection\b", re.I)
REP = re.compile(r"\bRep'[dy]\s+(?:by\s+)?(?P<a>.+)$", re.I)
EXPIRY = re.compile(r"\b(?:will be|he'?ll be|be)\s+(?:an?\s+)?(?:group[\s-]?6\s+)?(?P<s>UFA|RFA)\b"
                    r"|\b(?P<s2>UFA|RFA)\s+(?:upon|on|at)\s+expiry\b", re.I)
PAST = re.compile(r"\b(?:was|were|would have|had|previously|final year of|terminating)\b", re.I)
REPLICATE = re.compile(r"same breakdown|\bboth\s+(?:seasons?|years)\b|\beach\s*(?:season|year|yr)\b|"
                       r"\ball\s+(?:\d\s+)?(?:seasons|years)\b", re.I)
FLAT_AMOUNT = re.compile(
    r"(?P<amt>\$?\s?\d+(?:\.\d+)?\s?[KkMm]?)\s*(?:salary\s+|sal\s+)?(?:flat\b|each\s*(?:year|season|yr)\b|"
    r"both\s+(?:seasons?|years)\b|all\s+(?:\d\s+)?(?:seasons|years)\b)",
    re.I,
)
QO = re.compile(r"\$?\s?\d+(?:\.\d+)?\s?[KkMm]?\s*QO\b|QO\s+of\s+\$?\s?\d+(?:\.\d+)?\s?[KkMm]?", re.I)
ITEM_SPLIT = re.compile(r",\s*(?=\$)|,\s+|\s*/\s*|\s+&\s+|\.\s+|\s+and\s+|;\s*")
FILLER = re.compile(r"\b(?:deal|is|contract|flat|both|seasons?|years?|each|all|with|the|a|in|potential|earned|"
                    r"no)\b", re.I)
LABELS: list[tuple[str, re.Pattern[str]]] = [
    ("base", re.compile(r"(?:nhl|nh)(?:\s+(?:salary|sal))?|salary|sal|s|base(?:\s+salary)?")),
    ("sb", re.compile(r"sign(?:ing)?\s+bonus(?:es)?|sb")),
    ("pb", re.compile(r"(?:gp|games?[\s-]played)[\s-]?(?:bonus|b)|gpb|pb|(?:performance|perf)\s+bonus(?:es)?|"
                      r"bonus\s+total")),
    ("minors", re.compile(r"minors?(?:\s+salary)?|min|ahl(?:\s+salary)?")),
    ("guaranteed", re.compile(r"guaranteed")),
]
NAME_TOKEN = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'.\-À-ÿ]*$")
PARTICLES = {"van", "de", "der", "von", "da", "di", "du", "la", "le"}
NAME_STOP = {
    "the", "breakdown", "details", "expecting", "deal", "deals", "contract", "contracts", "extension", "ext",
    "cap", "hit", "aav", "elc", "pto", "nhl", "ntc", "nmc", "rfa", "ufa", "ncaa", "ncca", "fa", "ra", "f", "d",
    "g", "c", "ld", "rd", "lw", "rw", "f/d", "undrafted", "free", "agent", "pending", "yr", "yrs", "year",
    "years", "entry", "level", "round", "rounder", "pick", "salary", "base", "full", "includes", "with",
    "signs", "signed", "sign", "group", "recently", "acquired", "terminated", "x", "one", "two", "three",
    "four", "five", "six", "seven", "eight", "a", "an", "of", "is", "to",
}
DRAFT = re.compile(r"'\d\d\s+(?:\d+(?:st|nd|rd|th)\s+(?:round(?:er)?(?:\s+pick)?)|pick\s+#\d+|#\d+)|"
                   r"\bpick\s+#\d+|#\d+|\d+\s*y/o|y/o", re.I)
LINK = re.compile(r"https?://(?:www\.)?puckpedia\.com/[^\s]+", re.I)
MAX_MINORS = 2_000_000
MAX_SEASON_PART = 20_000_000
MAX_ELC_SALARY = 1_500_000


# --- text helpers --------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Straighten quotes, fix the typos and glued shorthand seen in real posts."""
    t = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("$$", "$")
    t = re.sub(r"(?<=[A-Za-z.])yr\s?s?\s(?=\d)", "\nYr ", t)          # "Salyr 2 3.5M" -> "Sal\nYr 2 3.5M"
    t = re.sub(r"(?<=[A-Za-z0-9])(NTC|NMC)", r" \1", t)                # "teamNTCclause"
    t = re.sub(r"(NTC|NMC)(?=[A-Za-z])", r"\1 ", t)
    t = re.sub(r"\bCal\s+Hit\b", "Cap Hit", t, flags=re.I)
    t = re.sub(r"\bSinging\b", "Signing", t)
    t = re.sub(r"\bcontact\b", "contract", t, flags=re.I)
    t = re.sub(r"\b(\d+)\s*y/o(?=[A-Z])", r"\1 y/o ", t)                 # "31 y/oF"
    return t


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def money(m: re.Match[str]) -> tuple[int, list[str]]:
    """Value of an AMT match. A bare number under 20 is millions ("$4.5 cap hit"), 20-999 thousands."""
    num = float(m["num"].replace(",", ""))
    unit = (m["unit"] or m["word"] or "").lower()
    if unit in ("m", "million"):
        return int(round(num * 1_000_000)), []
    if unit == "k":
        return int(round(num * 1_000)), []
    if "," in m["num"] or num >= 1000:
        return int(round(num)), []
    if num < 20:
        return int(round(num * 1_000_000)), ["unit_assumed"]
    return int(round(num * 1_000)), ["unit_assumed"]


def _amounts(s: str) -> list[re.Match[str]]:
    """Dollar-like numbers in ``s``; bare numbers glued to letters ("10GP", "1yr") are skipped."""
    out = []
    for m in AMT.finditer(s):
        if not (m["dollar"] or m["unit"] or m["word"]):
            after = s[m.end():m.end() + 6]
            if re.match(r"[A-Za-z%]|\s*(?:team|GP|P\b|G\b|NHL games|games|y/o)", after):
                continue
            if s[max(0, m.start() - 1):m.start()] in ("#", "'"):
                continue
        out.append(m)
    return out


def team_tags(line: str) -> tuple[list[str], list[str]]:
    """(mapped tri-codes, unmapped tags) for the hashtags in ``line``, in order."""
    teams: list[str] = []
    unmapped: list[str] = []
    for tag in HASHTAG.findall(line):
        key = tag.lower()
        if key in HASHTAG_TEAMS:
            if HASHTAG_TEAMS[key] not in teams:
                teams.append(HASHTAG_TEAMS[key])
        elif key not in NON_TEAM_TAGS:
            unmapped.append(tag)
    return teams, unmapped


def term_of(line: str) -> int | None:
    m = TERM.search(line)
    if not m:
        return None
    word = (m["t"] or m["x"] or m["e"]).lower()
    return NUMBER_WORDS.get(word)


def rkey_of(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def post_url(uri: str) -> str:
    return POST_URL.format(rkey=rkey_of(uri))


def ref_link(text: str, links: Iterable[str]) -> str | None:
    """The puckpedia.com link of a post (signing page preferred), query string dropped."""
    found = [u for u in links if "puckpedia.com" in u]
    found += [u for u in LINK.findall(text) if not u.endswith(("...", "…"))]
    found = [u.rstrip(").,") for u in found]
    if not found:
        return None
    found.sort(key=lambda u: (0 if "/signing/" in u else 1 if "/player/" in u else 2))
    return found[0].split("?", 1)[0]


# --- classification --------------------------------------------------------------

def _header(lines: list[str]) -> str:
    return lines[0] if lines else ""


def _season_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines[1:] if YR.match(ln.lstrip("-* ")) or SEASON_LABEL.match(ln)]


def _is_signing(lines: list[str]) -> bool:
    head = _header(lines)
    second = lines[1] if len(lines) > 1 else ""
    if NOT_SIGNING.search(head):
        return False
    teams, _ = team_tags(head)
    if not teams and not [t for t in HASHTAG.findall(head) if t.lower() not in NON_TEAM_TAGS]:
        return False
    has_term = (term_of(head) is not None or term_of(second) is not None or bool(_season_lines(lines))
                or bool(REMAINDER.search(head)))
    return has_term and bool(SIGN_VERB.search(head) or CONTRACT_WORD.search(head) or CAP_AFTER.search(head))


def classify_post(text: str) -> str:
    """"signing" | "trade" | "waivers" | "ltir" | "other", decided from the header line."""
    lines = _lines(normalize_text(text))
    if not lines:
        return "other"
    head = lines[0]
    if TRADE_HEAD.search(head):
        return "trade"
    if re.search(r"\bLTIR\b", head):
        return "ltir"
    if re.search(r"\bwaivers?\b|\bwaived\b|\bclaimed off\b", head, re.I):
        return "waivers"
    if _is_signing(lines):
        return "signing"
    return "other"


# --- names -----------------------------------------------------------------------

def _name_run(tokens: list[str], *, trailing: bool) -> str:
    """A run of capitalised name tokens (particles allowed inside): the last run when
    ``trailing`` (verb form: "...F Marat Khusnutdinov"), else the first one."""
    runs: list[list[str]] = []
    cur: list[str] = []
    for raw in tokens:
        tok = re.sub(r"'s$", "", raw.strip(",:;()"))
        if not re.fullmatch(r"(?:[A-Z]\.)+", tok):          # keep "J.G.", drop a sentence full stop
            tok = tok.rstrip(".")
        if (NAME_TOKEN.match(tok) and tok.lower() not in NAME_STOP) or (cur and tok.lower() in PARTICLES):
            cur.append(tok)
            if not raw.endswith((",", ":", ";", "'s")):
                continue
        if cur:
            runs.append(cur)
        cur = []
    if cur:
        runs.append(cur)
    runs = [r[:-1] if r[-1].lower() in PARTICLES else r for r in runs]
    runs = [r for r in runs if r]
    if not runs:
        return ""
    return " ".join(runs[-1] if trailing else runs[0])


def player_name(head: str) -> str:
    """The player named in the header, as written (accents kept)."""
    m = re.search(r"\b(?:re-?)?(?:sign(?:s|ed|ing)?|singed)\s+(?P<pre>.+?)\s+to\s+(?=\S)", head, re.I)
    if m:
        pre = DRAFT.sub(" ", m["pre"])
        pre = HASHTAG.sub(" ", pre)
        name = _name_run(pre.split(), trailing=True)
        if name or not re.search(r"[A-Za-z]{3}", pre):
            return name
    rest = re.sub(r"^(?:the\s+|breakdown\s+of\s+|details\s+of\s+)+", "", head, flags=re.I)
    rest = HASHTAG.sub(" ", DRAFT.sub(" ", rest))
    rest = AMT.sub(" ", rest)
    return _name_run(rest.split(), trailing=False)


def _expand_name(name: str, links: list[str], flags: list[str]) -> str:
    """Fill a missing first name from the post's puckpedia.com/player/<slug> link."""
    slug = next((m for u in links for m in [re.search(r"puckpedia\.com/player/([a-z0-9\-]+)", u)] if m), None)
    if not name:
        if slug:
            flags.append("name_from_link")
            return " ".join(p.capitalize() for p in slug.group(1).split("-"))
        return ""
    if " " in name:
        return name
    flags.append("surname_only")
    if slug:
        parts = slug.group(1).split("-")
        key = name_key(name).replace(" ", "")
        for k in range(1, len(parts)):
            if "".join(parts[k:]) == key:
                flags.append("first_name_from_link")
                return " ".join(p.capitalize() for p in parts[:k]) + " " + name
    return name


# --- money in the header and summary lines ----------------------------------------

def _cap_and_aav(lines: list[str], term_line: str, flags: list[str]) -> tuple[int | None, int | None]:
    cap: int | None = None
    aav: int | None = None
    for ln in lines:
        if PAST.search(ln) or QO.search(ln) or ln.lower().startswith("rep'"):
            continue
        for m in _amounts(ln):
            after = ln[m.end():m.end() + 30]
            before = ln[max(0, m.start() - 25):m.start()]
            value, f = money(m)
            ca = CAP_AFTER.match(after.lstrip(" "))
            if ca:
                if cap is None:
                    cap = value
                    flags += f
                if ca["aav"] and aav is None:
                    aav = value
                continue
            cb = CAP_BEFORE.search(before)
            if cb:
                if cap is None:
                    cap = value
                    flags += f
                if cb["aav"] and aav is None:
                    aav = value
                continue
            if re.match(r"\s*AAV\b", after) or re.search(r"\bAAV[ \t]*:?[ \t]*$", before):
                if aav is None:
                    aav = value
                    flags += f
    if cap is None and aav is None:
        # "1 year $850K deal", "8 yr @ $6.75M", "5 x $5M": the figure right after the term
        m = TERM.search(term_line)
        if m:
            tail = term_line[m.end() - (1 if m["x"] else 0):]
            tail = re.sub(r"^[\s,]*(?:(?:1|one|2|two)[\s-]?way\s+)?(?:@|at)?\s*", "", tail, flags=re.I)
            nxt = _amounts(tail)
            if nxt and nxt[0].start() == 0:
                cap, f = money(nxt[0])
                flags += f + ["cap_hit_unlabeled"]
    return cap, aav


# --- components: "Yr 1: $850K NHL, $102.5K SB" and "NHL: $850K" ---------------------

def _label(rest: str) -> str | None:
    r = rest.lower().replace('"', " ").replace("'", " ")
    r = r.split(" - ")[0]
    r = FILLER.sub(" ", r)
    r = re.sub(r"[^a-z\s\-]", " ", r)
    r = re.sub(r"\s+", " ", r).strip(" -")
    if not r:
        return ""
    for kind, pat in LABELS:
        if pat.fullmatch(r):
            return kind
    return None


def _items(segment: str) -> list[dict[str, Any]]:
    """Split a breakdown segment into items: {value, kind, one_way, clause, flags, raw}."""
    out = []
    for raw in ITEM_SPLIT.split(segment):
        raw = raw.strip(" ,.;:")
        if not raw:
            continue
        item: dict[str, Any] = {"raw": raw, "value": None, "kind": None, "one_way": None, "clause": None,
                                "flags": []}
        if CLAUSE_WORD.search(raw):
            item["clause"] = raw
            out.append(item)
            continue
        body = raw
        if ONE_WAY.search(body):
            item["one_way"] = True
            body = ONE_WAY.sub(" ", body)
        elif TWO_WAY.search(body):
            item["one_way"] = False
            body = TWO_WAY.sub(" ", body)
        body = body.split(" - ")[0] if _amounts(body.split(" - ")[0]) else body
        ams = _amounts(body)
        if len(ams) != 1:
            if ams:
                item["flags"].append("unparsed_item")
            out.append(item)
            continue
        item["value"], item["flags"] = money(ams[0])
        item["kind"] = _label(body[:ams[0].start()] + " " + body[ams[0].end():])
        out.append(item)
    return out


DROP_ORDER = ("pb", "guaranteed", "sb", "minors", "base")


def _assign(items: list[dict[str, Any]], schema: list[str]) -> list[str]:
    """Give unlabeled values the labels of Yr 1 by position ("$900K/$107.5K/$85K")."""
    flags: list[str] = []
    vals = [i for i in items if i["value"] is not None]
    labeled = {i["kind"] for i in vals if i["kind"]}
    unl = [i for i in vals if not i["kind"]]
    slots = [s for s in schema if s not in labeled]
    while len(slots) > len(unl):
        slots.remove(next(d for d in DROP_ORDER if d in slots))
    if len(unl) > len(slots):
        flags.append("unparsed_season_values")
    for item, slot in zip(unl, slots, strict=False):
        item["kind"] = slot
    return flags


def _schema(items: list[dict[str, Any]], is_elc: bool) -> list[str]:
    vals = [i for i in items if i["value"] is not None]
    default = ["base", "sb", "pb", "minors"] if is_elc else ["base", "sb", "minors", "guaranteed"]
    schema: list[str] = []
    for i in vals:
        kind = i["kind"] or next((d for d in default if d not in schema), "base")
        schema.append(kind)
    return schema


def _segments(line: str) -> list[tuple[list[int] | None, str, str]]:
    """Split a season line into (year numbers, season label, body) segments."""
    lab = SEASON_LABEL.match(line)
    if lab:
        a = int(lab.group(1))
        return [(None, f"20{a:02d}-{(a + 1) % 100:02d}", line[lab.end():])]
    marks = [m for m in YR.finditer(line)]
    if not marks or marks[0].start() > 2:
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
        a = int(m["a"])
        b = int(m["b"]) if m["b"] else a
        out.append((list(range(a, b + 1)) if b >= a else [a], "", line[m.end():end]))
    return out


# --- clauses -------------------------------------------------------------------------

def _clause_kind(m: re.Match[str]) -> tuple[str, int | None]:
    kind = m["kind"].upper().replace(" ", "")
    n = int(m["n"]) if m["n"] else None
    if kind in ("NMC", "MNC") or kind.startswith("NO-MOVE") or kind.startswith("NOMOVE"):
        return "NMC", None
    if n:
        return "M-NTC", n
    return "NTC", None


def _range(phrase: str, term: int | None) -> tuple[int, int] | None:
    m = THROUGH_YR.search(phrase)
    if m:
        return 1, int(m["b"])
    m = YR.search(phrase)
    if m:
        a = int(m["a"])
        return a, int(m["b"]) if m["b"] else a
    m = ORDINAL_YR.search(phrase)
    if m:
        return int(m["a"]), int(m["a"])
    if WHOLE_TERM.search(phrase) and term:
        return 1, term
    return None


def _clauses(lines: list[str], term: int | None, flags: list[str]
             ) -> list[tuple[str, int | None, tuple[int, int] | None, str]]:
    """(kind, list size, year range, raw phrase) for every trade-protection phrase."""
    out = []
    for ln in lines:
        if ln.lower().startswith("rep'"):
            continue
        segs = _segments(ln)
        pieces = [(yrs, body) for yrs, _, body in segs] if segs else [(None, ln)]
        for yrs, body in pieces:
            for phrase in re.split(r",\s+|\.\s+|;\s*|\s+&\s+(?![1-8]\b(?!\s*-?\s*team))|\bthen\b", body):
                for m in CLAUSE.finditer(phrase):
                    if re.match(r"no[\s-]trade\s+clauses\b", m["kind"], re.I) and not (m["n"] or m["full"]):
                        flags.append("clause_ambiguous")
                        continue
                    kind, n = _clause_kind(m)
                    rng = (min(yrs), max(yrs)) if yrs else _range(phrase, term)
                    out.append((kind, n, rng, phrase.strip(" .")))
    return out


# --- the post --------------------------------------------------------------------------

def _start_season(text: str, published: date, is_ext: bool, flags: list[str]) -> str:
    m = START.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if (a + 1) % 100 != b:
            flags.append("season_typo")
        return f"20{a:02d}-{(a + 1) % 100:02d}"
    current = season_from_date(published)
    if NEXT_SEASON.search(text):
        flags.append("derived:start_next_season")
        return shift_season(current, 1)
    flags.append("start_season_assumed")
    # An extension starts after the current deal; a deal signed in May/June (after the
    # regular season) is for next season.
    if is_ext or published.month in (5, 6):
        return shift_season(current, 1)
    return current


def _agent(lines: list[str]) -> str | None:
    for ln in lines:
        m = REP.search(ln)
        if m:
            a = re.sub(r"https?://\S+|@\s?[\w.]+", " ", m["a"])
            a = re.sub(r"\s+", " ", a).strip(" ,&-/")
            return a if re.search(r"[A-Za-z]", a) else None
    return None


def _expiry(lines: list[str], flags: list[str]) -> str | None:
    for ln in lines[1:]:
        if PAST.search(ln):
            continue
        if re.search(r"\b(?:unless|if)\b", ln, re.I) and re.search(r"\b(?:UFA|RFA)\b", ln):
            flags.append("expiry_conditional")
            return None
        m = EXPIRY.search(ln)
        if m and (re.search(r"expir|next summer", ln, re.I) or m["s2"]):
            if re.search(r"\barb", ln, re.I):
                flags.append("rfa_arbitration_rights")
            if re.search(r"group[\s-]?6", ln, re.I):
                flags.append("group6_ufa")
            return (m["s"] or m["s2"]).upper()
    return None


def _suspicious(kind: str, value: int | None, is_elc: bool) -> bool:
    if value is None:
        return False
    if kind == "minors" and value > MAX_MINORS:
        return True
    if kind == "base" and is_elc and value > MAX_ELC_SALARY:
        return True
    return value > MAX_SEASON_PART


def _year_rows(lines: list[str], is_elc: bool, flags: list[str]
               ) -> tuple[dict[int, dict[str, Any]], str | None, bool]:
    """Rows from "Yr N:" / "26-27:" lines -> ({year number: row}, stated first season, any Yr lines)."""
    rows: dict[int, dict[str, Any]] = {}
    segs = [seg for ln in _season_lines(lines) for seg in _segments(ln.lstrip("-* "))]
    first = next((label for _, label, _ in segs if label), None)
    schema: list[str] | None = None
    for yrs, label, body in segs:
        if label and first:
            yrs = [int(label[:4]) - int(first[:4]) + 1]
        items = _items(body)
        values = [i for i in items if i["value"] is not None]
        if not values or not yrs:
            continue
        if schema is None:
            schema = _schema(items, is_elc)
            if any(not i["kind"] for i in values) and len(values) > 1:
                flags.append("positional_guess")
            for item, kind in zip(values, schema, strict=False):
                item["kind"] = item["kind"] or kind
        else:
            flags += _assign(items, schema)
        row: dict[str, Any] = {"one_way": None, "clause": None, "evidence": body.strip()}
        for item in items:
            flags += item["flags"]
            if item["value"] is not None and item["kind"] and item["kind"] not in row:
                row[item["kind"]] = item["value"]
            if item["one_way"] is not None:
                row["one_way"] = item["one_way"]
            if item["clause"]:
                row["clause"] = item["clause"]
        for y in yrs:
            if y in rows:
                flags.append("duplicate_season")
            else:
                rows[y] = dict(row)
    return rows, first, bool(segs)


def _flat_rows(lines: list[str], term: int, cap: int | None, flags: list[str]
               ) -> tuple[dict[int, dict[str, Any]], dict[str, int]]:
    """Rows from one-season lines ("NHL: $850K", "$2.5M Salary") and flat salaries ("$2M salary flat
    both seasons"). Returns ({year number: row}, the one-season components)."""
    head, body_lines = lines[0], lines[1:]
    season = set(_season_lines(lines))
    flat: dict[str, int] = {}
    flat_amount: int | None = None
    used: list[str] = []
    replicate = bool(REPLICATE.search(head) and not CLAUSE_WORD.search(head))
    for ln in body_lines:
        if ln in season or REP.search(ln) or PAST.search(ln):
            continue
        body = QO.sub(" ", ln)
        fm = FLAT_AMOUNT.search(re.sub(r"\d+\s*-?\s*team\b", " ", body))
        if fm and flat_amount is None:
            am = _amounts(fm["amt"])
            if am:
                flat_amount, f = money(am[0])
                flags += f
                used.append(ln)
        got = 0
        for item in _items(body):
            if item["value"] is not None and item["kind"] and item["kind"] not in flat:
                flat[item["kind"]] = item["value"]
                flags += item["flags"]
                got += 1
        if got:
            used.append(ln)
        if REPLICATE.search(ln) and not CLAUSE_WORD.search(ln) and (got or re.search("same breakdown", ln, re.I)):
            replicate = True
    if flat_amount is not None and flat.get("base") in (None, flat_amount):
        flat["base"] = flat_amount
        replicate = True
    text = "\n".join(lines)
    rows: dict[int, dict[str, Any]] = {}
    row: dict[str, Any] = {"one_way": None, "clause": None, "evidence": " / ".join(dict.fromkeys(used))[:300]}
    if flat and (term == 1 or replicate):
        row.update(flat)
        rows = {y: dict(row) for y in range(1, term + 1)}
    elif not flat and cap and re.search(r"\ball sal(?:ary)?\b", text, re.I):
        if term == 1:
            rows = {1: {**row, "base": cap, "evidence": "all salary"}}
            flags.append("derived:all_salary_from_cap_hit")
        elif re.search(r"\bflat\b", text, re.I):
            rows = {y: {**row, "base": cap, "evidence": "all salary, flat"} for y in range(1, term + 1)}
            flags.append("derived:flat_salary_from_cap_hit")
    return rows, flat


def _one_way(head: str, lines: list[str], rows: dict[int, dict[str, Any]], flags: list[str]) -> bool | None:
    """Contract-level one-way flag (as stated, else from the seasons); fills season rows too."""
    for r in rows.values():
        if r["one_way"] is None and r.get("minors") is not None:
            r["one_way"] = False
    stated: bool | None = None
    if TWO_WAY.search(head):
        stated = False
    elif ONE_WAY.search(head) or any(ONE_WAY.search(ln) and not YR.match(ln) for ln in lines[1:]):
        stated = True
    ways = {r["one_way"] for r in rows.values() if r["one_way"] is not None}
    if len(ways) > 1:
        flags.append("one_way_varies_by_season")
        return stated
    if stated is None and ways:
        stated = next(iter(ways))
        if stated is False:
            flags.append("derived:two_way_from_minors")
    if stated is not None and len(ways) <= 1:
        for r in rows.values():
            if r["one_way"] is None:
                r["one_way"] = stated
    return stated


def _clause_rows(lines: list[str], term: int | None, start: str, cid: str, key: str, source_id: str,
                 flags: list[str]) -> tuple[list[ClauseClaim], dict[int, list[str]]]:
    clauses: list[ClauseClaim] = []
    by_year: dict[int, list[str]] = {}
    for kind, n, rng, phrase in _clauses(lines, term, flags):
        lo, hi = rng if rng else (None, None)
        clauses.append(ClauseClaim(
            claim_id=cid, contract_key=key, kind=kind,
            season_from=shift_season(start, lo - 1) if lo else None,
            season_to=shift_season(start, hi - 1) if hi else None,
            list_size=n, list_type=None, source_id=source_id, confidence_tier=TIER, evidence=phrase,
        ))
        if lo and hi:
            for y in range(lo, hi + 1):
                by_year.setdefault(y, []).append(f"{n} team {kind}" if n else kind)
    return clauses, by_year


def _evidence(raw_text: str, ref: str | None) -> str:
    ev = re.sub(r"(?:\s*\n\s*)+", " / ", raw_text.strip())
    return f"{ev} / {ref}" if ref and ref not in ev else ev


def extract_post(post: dict[str, Any]) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], Source | None]:
    """One PuckPedia post -> (claims, season rows, clause rows, source). Non-signings yield nothing."""
    raw_text = post.get("text") or ""
    if classify_post(raw_text) != "signing":
        return [], [], [], None
    text = normalize_text(raw_text)
    lines = _lines(text)
    head = lines[0]
    created = post.get("created_at") or ""
    published = date.fromisoformat(created[:10])
    flags: list[str] = []

    ref = ref_link(raw_text, post.get("links") or [])
    if ref:
        flags.append(f"ref:{ref}")
    if head.lower().startswith("expecting"):
        flags.append("expected_not_final")
    teams, unmapped = team_tags(head)
    team = teams[0] if teams else None
    if team is None:
        flags += [f"team_unmapped:#{t}" for t in unmapped] or ["team_unmapped"]
    links = list(post.get("links") or []) + LINK.findall(raw_text)
    name = _expand_name(player_name(head), links, flags)
    if not name:
        return [], [], [], None

    term_line = head if term_of(head) is not None else (lines[1] if len(lines) > 1 else "")
    term = term_of(term_line)
    is_elc = bool(ELC.search("\n".join(lines[:2])))
    is_ext = bool(EXT.search(head))
    ctype = "PTO" if PTO.search(head) else "ELC" if is_elc else "extension" if is_ext else "standard"
    cap, aav = _cap_and_aav(lines, term_line, flags)

    rows, first_season, had_yr = _year_rows(lines, is_elc, flags)
    if term is None and rows and set(rows) == set(range(1, len(rows) + 1)):
        term = len(rows)
        flags.append("derived:term_from_seasons")
    if term is None and REMAINDER.search(head):
        term = 1
        flags.append("derived:term_remainder_of_season")
    flat: dict[str, int] = {}
    if not rows and term:
        rows, flat = _flat_rows(lines, term, cap, flags)
    one_way = _one_way(head, lines, rows, flags)
    if one_way is None and not rows and flat.get("minors") is not None:
        one_way = False
        flags.append("derived:two_way_from_minors")

    start = first_season or _start_season(text, published, is_ext, flags)
    end = shift_season(start, term - 1) if term else None
    nhl_salary = minors_salary = None
    yr1 = rows.get(1, {})
    if yr1.get("minors") is not None:
        nhl_salary, minors_salary = yr1.get("base"), yr1["minors"]
        if had_yr and term and term > 1:
            flags.append("salary_from_yr1")
    elif flat.get("minors") is not None:
        nhl_salary, minors_salary = flat.get("base"), flat["minors"]
    guaranteed = [(y, r["guaranteed"]) for y, r in sorted(rows.items()) if r.get("guaranteed") is not None]
    if term == 1 and guaranteed:
        flags.append(f"guaranteed:{guaranteed[0][1]}")
    else:
        flags += [f"guaranteed:{shift_season(start, y - 1)}:{g}" for y, g in guaranteed]
    if flat.get("guaranteed") is not None and not guaranteed:
        flags.append(f"guaranteed:{flat['guaranteed']}")
    if any(_suspicious(k, r.get(k), is_elc) for r in rows.values() for k in ("base", "sb", "pb", "minors")):
        flags.append("suspicious_amount")

    complete = bool(term) and set(rows) == set(range(1, (term or 0) + 1)) and all(
        r.get("base") is not None for r in rows.values())
    if rows and not complete:
        flags.append("season_breakdown_incomplete")
    total = None
    if complete and (had_yr or (term or 0) > 1) and "suspicious_amount" not in flags:
        total = sum(r["base"] + (r.get("sb") or 0) for r in rows.values())
        flags.append("derived:total_from_seasons")
        if cap and term and term > 1 and not is_elc and abs(total - cap * term) > max(0.02 * total, 60_000):
            flags.append("total_vs_cap_hit_mismatch")

    expiry = _expiry(lines, flags)
    agent = _agent(lines)
    source = Source(
        source_id=f"bsky:{rkey_of(post['uri'])}", url=post_url(post["uri"]), publisher=PUBLISHER,
        author="PuckPedia", published_at=created, confidence_tier=TIER, headline=head[:200], kind=KIND,
    )
    cid = claim_id(source.source_id, name, None)
    key = make_key(None, name, team, published.isoformat())
    clauses, clause_years = _clause_rows(lines, term, start, cid, key, source.source_id, flags)
    if not clauses and NO_PROTECTION.search(text):
        flags.append("no_trade_protection")

    claim = Claim(
        claim_id=cid, contract_key=key, player_name=name, player_id=None, team=team,
        signed_at=published.isoformat(), contract_type=ctype, term_years=term, aav_usd=aav, cap_hit_usd=cap,
        total_usd=total, nhl_salary_usd=nhl_salary, minors_salary_usd=minors_salary, one_way=one_way,
        start_season=start, end_season=end, re_signed=bool(re.search(r"\bre-?sign", head, re.I)),
        source_id=source.source_id, source_url=source.url, publisher=PUBLISHER, published_at=created,
        confidence_tier=TIER, extracted_by="regex", flags=list(dict.fromkeys(flags)),
        evidence=_evidence(raw_text, ref), expiry_status=expiry, agent=agent, signing_date_stated=False,
    )
    seasons = [
        SeasonClaim(
            claim_id=cid, contract_key=key, season=shift_season(start, y - 1), cap_hit_usd=cap, aav_usd=aav,
            base_salary_usd=r.get("base"), signing_bonus_usd=r.get("sb"), performance_bonus_usd=r.get("pb"),
            total_salary_usd=r["base"] + r["sb"] if r.get("base") is not None and r.get("sb") is not None else None,
            minors_salary_usd=r.get("minors"), one_way=r["one_way"],
            clause=r["clause"] or ("; ".join(clause_years[y]) if y in clause_years else None),
            source_id=source.source_id, confidence_tier=TIER, evidence=r["evidence"][:300],
        )
        for y, r in sorted(rows.items())
    ]
    return [claim], seasons, clauses, source


def normalize_posts(posts: Iterable[dict[str, Any]]
                    ) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], list[Source]]:
    """Every stored post (``Store.bsky_posts``) -> claims, season rows, clause rows, sources."""
    claims: list[Claim] = []
    seasons: list[SeasonClaim] = []
    clauses: list[ClauseClaim] = []
    sources: list[Source] = []
    for post in posts:
        c, s, k, src = extract_post(post)
        claims += c
        seasons += s
        clauses += k
        if src is not None:
            sources.append(src)
    return claims, seasons, clauses, sources
