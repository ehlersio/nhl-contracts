"""Regex extraction of contract facts from r/hockey post titles (tier D).

r/hockey titles quote insider and club posts verbatim, with the quoted account
in a leading bracket tag::

    [Milstein] Kirill Marchenko has agreed to terms on a six year, $75 million contract extension ...
    [NY Rangers PR] The Rangers have signed forward Luke Glendening to a professional tryout agreement (PTO).
    Dallas Stars sign Goaltender Casey DeSmith to a two-year, $5 million contract extension (2x$2.5m AAV)
    Report: Bruins signing Khusnutdinov to 3-year, $11.85M extension

The Reddit post is the cited source (its permalink); the bracket tag, when
present, is the author. Only titles that report a completed signing become
claims (:func:`classify_title`): rumours ("closing in on", "expected to"),
questions, staff deals (coaches, GMs, EBUGs, broadcasters) and other leagues
yield nothing. Posts quoting PuckPedia (read directly as its own source) or
CapWages/Spotrac/CapFriendly (off-limits sites) are skipped.

Values stay as written: "N x $A" is a per-year figure (AAV), never multiplied
into a total; a total is taken only when the title states one ("$75 million
contract", "worth $36,250,000"). A signing that states neither a number of years
nor an amount (and is not a PTO) is dropped: in practice those are staff deals
announced as "multi-year". Season rows and clauses are emitted only when the
title literally carries "Yr 1 ..." or NMC/NTC wording.

Output: :class:`Claim`, :class:`SeasonClaim`, :class:`ClauseClaim` and
:class:`Source` rows from ``model.py``, all with ``confidence_tier`` "D".
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import date
from typing import Any
from urllib.parse import urlparse

from . import puckpedia as pp
from .contracts import TEAM_ALIASES, TEAM_NICKNAMES, team_from_text
from .model import Claim, ClauseClaim, SeasonClaim, Source, claim_id, make_key
from .text import MONEY, NUMBER_WORDS, clean, money_value, shift_season, strip_accents, term_years

TIER = "D"
KIND = "reddit_hockey"
PUBLISHER = "r/hockey post"

# --- teams -----------------------------------------------------------------------
EXTRA_ALIASES = {"Sens": "OTT", "Nucks": "VAN", "Bolts": "TBL", "Preds": "NSH"}
CITIES: dict[str, str] = {
    "Anaheim": "ANA", "Boston": "BOS", "Buffalo": "BUF", "Calgary": "CGY", "Carolina": "CAR",
    "Chicago": "CHI", "Colorado": "COL", "Columbus": "CBJ", "Dallas": "DAL", "Detroit": "DET",
    "Edmonton": "EDM", "Florida": "FLA", "Los Angeles": "LAK", "Minnesota": "MIN", "Montreal": "MTL",
    "Nashville": "NSH", "New Jersey": "NJD", "NY Rangers": "NYR", "NY Islanders": "NYI", "Ottawa": "OTT",
    "Philadelphia": "PHI", "Pittsburgh": "PIT", "San Jose": "SJS", "Seattle": "SEA", "St. Louis": "STL",
    "Tampa Bay": "TBL", "Toronto": "TOR", "Utah": "UTA", "Vancouver": "VAN", "Vegas": "VGK",
    "Washington": "WSH", "Winnipeg": "WPG",
}
TEAM_WORDS = {
    w.lower()
    for name in [*TEAM_NICKNAMES.values(), *TEAM_ALIASES, *EXTRA_ALIASES, *CITIES, "New York", "NY", "Las Vegas"]
    for w in name.replace(".", "").split()
}

# --- vocabulary --------------------------------------------------------------------
TAG = re.compile(r"^\s*\[(?P<tag>[^\]]{1,80})\]\s*:?\s*")
PREFIX = re.compile(r"^(?P<p>Report|RELEASE|Release|BREAKING|Breaking|Official|OFFICIAL|Confirmed|CONFIRMED)"
                    r"\s*[:\-]\s*")
SKIP_TAG = re.compile(r"puck\s*pedia|cap\s*wages|spotrac|cap\s*friendly|stanley\s*cap", re.I)
SKIP_DOMAIN = re.compile(r"(?:^|\.)(?:puckpedia\.com|capwages\.com|spotrac\.com|capfriendly\.com|thestanleycap\.com)$")
NON_NHL_DOMAIN = re.compile(r"(?:^|\.)(?:thepwhl\.com|khl\.ru|shl\.se|liiga\.fi|theahl\.com|echl\.com|"
                            r"hartfordwolfpack\.com|lausannehc\.ch|swisshockeynews\.ch)$")
SCORE_TAG = re.compile(r"\(\d+\)|\d+\s*-\s*\(?\d")
NON_NEWS_TAG = re.compile(r"^(?:highlight|lowlight|serious(?: question)?|discussion|oc|meta|poll|video|gif|"
                          r"game thread|post game|stat|stats)$", re.I)
TEAM_ACCOUNT = re.compile(r"\bPR\b|\bon (?:X|Twitter|Bluesky|Instagram|Threads)\b|^NHL(?:\.com)?$|^NHLPA$", re.I)

CONTRACT_MENTION = re.compile(
    r"\b(?:re-?)?sign(?:s|ed|ing)?\b|\bextensions?\b|\bcontracts?\b|\bagree(?:s|d)?\s+to\s+terms\b|\bPTO\b"
    r"|\bAAV\b|\bcap\s+hit\b|\d\s*x\s*\$|\binks?\b|\binked\b",
    re.I,
)
QUESTION = re.compile(r"\?")
QUOTE_LEAD = re.compile(r"^[\"']")  # an interview quote, not a transaction
RUMOUR = re.compile(
    r"closing in|\bclose to\b|\bcloser to\b|\bexpected to\b|\bexpecting\b|\bexpects?\b|\bnearing\b|\bnear(?:ly)? an?\b"
    r"|\bin talks\b|\btalks?\b|negotiat|\bworking on\b|\bsounds like\b|\bpermission to\b|\bhas not\b|\bhave not\b"
    r"|\bhasn't\b|\bhaven't\b|\bnot yet\b|\bno signs?\b|\binterest(?:ed)?\b|\brumou?r|\bpotential(?:ly)?\b"
    r"|\bpossible\b|\blikely\b|\btrying to\b|\bwants? to\b|\bhop(?:e|es|ing)\b|\bplans? to\b|\bset to\b"
    r"|\bpoised to\b|\b(?:will|would|could|might|should|can)\s+(?:re-?)?sign\b|\bcould\b|\bwould\b|\bmight\b"
    r"|\bdiscuss",
    re.I,
)
NOT_SIGNING = re.compile(
    r"\bpending\s+[UR]FA\b|\bscor(?:es|ed|ing)\b|\bgoals?\b|\bassists?\b|\bfresh off\b|\breleased?\s+from\b"
    r"|\breleased\b|\bwaive|\bwaivers\b|\bterminat|\bbuy[\s-]?out|\bbought out\b|\btrad(?:e|ed|es|ing)\b"
    r"|\bacquir|\bassigned\b|\breassigned\b|\brecalled\b|\bloaned\b|\bhealthy scratch\b|\bsuspen",
    re.I,
)
STAFF = re.compile(
    r"\bcoach|\bGMs?\b|\bgeneral manager\b|\bassistant\b|\bpresident\b|\bscout|\bEBUG\b|\bemergency backup\b"
    r"|broadcast|play-by-play|\bvoice of\b|\banalyst\b|\btrainer\b|\bdirector\b|\bexecutive\b|\bcommissioner\b"
    r"|\bowner\b|\breferee\b|\blinesm[ae]n\b|\bcommentator\b|\bstaff\b",
    re.I,
)
STAFF_URL = re.compile(r"coach|general-manager|\bgm\b|-gm-|broadcaster|president|director|ebug", re.I)
NON_NHL_CS = re.compile(r"\b(?:PWHL|KHL|SHL|AHL|ECHL|NCAA|OHL|WHL|QMJHL|CHL|USHL|DEL|IIHF|HC|ZSC|HV71|NL)\b")
NON_NHL = re.compile(
    r"\bLiiga\b|\bNational League\b|\bLausanne\b|\bAjoie\b|\bGeneve\b|\bServette\b|\bWolf Pack\b"
    r"|\bArizona State\b|\bcollege\b|\buniversity\b|\bExtraliga\b|\bOlympic|\bWorld Juniors\b|\bwomen'?s\b"
    r"|\bGoldeneyes\b|\bSceptres\b|\bVictoire\b|\bSirens\b|\bFrolunda\b|\bLugano\b|\bDavos\b|\bFribourg\b"
    r"|\bKloten\b|\bLangnau\b|\bAmbri\b|\bRapperswil\b|\bMarlies\b|\bColorado Eagles\b|\bLaval Rocket\b"
    r"|\bHershey\b|\bjunior\b",
    re.I,
)

# Completed-signing verbs. "signing" counts only as "signing NAME to" in a report.
VERB = re.compile(
    r"\b(?P<verb>(?:(?:has|have)\s+(?:officially\s+)?)?(?:re-?)?signed"
    r"|(?:re-?)?signs?(?!\s+(?:bonus|that|of)\b)"
    r"|(?:(?:has|have)\s+)?agree(?:s|d)?\s+to\s+terms"
    r"|ink(?:s|ed)?"
    r"|announce(?:s|d)?(?=.*\b(?:extension|contract|deal)\b)"
    r"|signing(?=\s+(?-i:[A-Z])\S*(?:\s+(?-i:[A-Z])\S*){0,3}\s+to\b))\b",
    re.I,
)
NEGATED_VERB = re.compile(r"(?:\b(?:no|not|to|will|would|could|never)|n't)\s+(?:be\s+)?$", re.I)

ELC = re.compile(r"\bELC\b|\bentry[\s-]level\b", re.I)
EXT = re.compile(r"\bextensions?\b|\bextend(?:s|ed)?\b", re.I)
PTO = re.compile(r"\bPTO\b|\bprofessional tryout\b|\btryout (?:agreement|contract)\b", re.I)
ONE_WAY = re.compile(r"\b(?:1|one)[\s-]way\b", re.I)
TWO_WAY = re.compile(r"\b(?:2|two)[\s-]way\b", re.I)
MULTI_YEAR = re.compile(r"\bmulti-?year\b", re.I)
PER_X_BEFORE = re.compile(r"\b(?P<n>[1-8])\s*x\s*$", re.I)
WORD_TERM = re.compile(r"\b(?P<n>one|two|three|four|five|six|seven|eight|[1-8])[\s-]+years?\b", re.I)
FOR_SEASON = re.compile(r"\bfor\s+(?:the\s+)?(?P<y>20\d\d)-(?P<z>\d\d)\b(?!\s+season\s+only)", re.I)
THROUGH = re.compile(r"\bthrough\s+(?:the\s+)?(?P<y>20\d\d)-(?P<z>\d\d)\b", re.I)
YR_LINE = re.compile(r"\s+(?=Yr\s?[1-8]\b)", re.I)

AAV_AFTER = re.compile(r"^\s*(?:AAV|average annual value|annual average(?: value)?|per (?:year|season)"
                       r"|a (?:year|season)|annually)\b", re.I)
CAP_AFTER = re.compile(r"^\s*cap\s*hit\b", re.I)
AAV_BEFORE = re.compile(r"(?:AAV|average annual value|annual average(?: value)?)\s*(?:of|:)?\s*(?:about\s+)?$", re.I)
CAP_BEFORE = re.compile(r"cap\s*hit\s*(?:of|:)?\s*(?:about\s+)?$", re.I)
TOTAL_BEFORE = re.compile(r"\b(?:worth|totall?ing|valued at|total(?: value)? of)\s*(?:about\s+)?$", re.I)
TOTAL_AFTER = re.compile(r"^\s*(?:[\w-]+\s+){0,2}?(?:contracts?|deals?|extensions?|pacts?)\b", re.I)

NAME_TOKEN = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'.\-À-ÿ]*$")
PARTICLES = {"van", "de", "der", "von", "da", "di", "du", "la", "le"}
NAME_STOP = {
    "the", "a", "an", "to", "on", "with", "for", "of", "and", "in", "at", "from", "as", "his", "her", "by",
    "forward", "forwards", "defenseman", "defenceman", "defender", "defence", "defense", "goaltender",
    "goalie", "netminder", "center", "centre", "winger", "wing", "prospect", "veteran", "captain", "rookie",
    "free", "agent", "ufa", "rfa", "f", "d", "g", "c", "lw", "rw", "nhl", "pr", "contract", "contracts",
    "extension", "deal", "agreement", "tryout", "professional", "pto", "elc", "aav", "cap", "hit", "entry",
    "level", "year", "years", "multi-year", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "terms", "agree", "agrees", "agreed", "sign", "signs", "signed", "re-sign", "re-signs", "re-signed",
    "signing", "inks", "ink", "inked", "announce", "announces", "announced", "report", "release", "official",
    "breaking", "has", "have", "team", "club", "new", "former", "undrafted", "pending", "rights",
}
SKIP_BEFORE_NAME = {
    "a", "an", "the", "forward", "defenseman", "defenceman", "defender", "goaltender", "goalie", "netminder",
    "center", "centre", "winger", "wing", "prospect", "veteran", "rookie", "free", "agent", "ufa", "rfa",
    "f", "d", "g", "c", "lw", "rw", "undrafted", "former", "captain", "y/o",
}
TERMISH = re.compile(r"^(?:\d|one|two|three|four|five|six|seven|eight)[\s-]?(?:year|yr|way)s?,?$|^\d+$", re.I)


# --- title helpers -------------------------------------------------------------------

def _norm(title: str) -> str:
    t = clean(html.unescape(title or ""))
    t = re.sub(r"(?<=\d)-million\b", " million", t)
    return t.replace("\n", " ").strip()


def split_title(title: str) -> tuple[str | None, str | None, str]:
    """(bracket tag, prefix such as "Report", remaining text) of a title."""
    t = _norm(title)
    tag = None
    m = TAG.match(t)
    if m:
        tag = m["tag"].strip()
        t = t[m.end():]
    prefix = None
    p = PREFIX.match(t)
    if p:
        prefix = p["p"].capitalize()
        t = t[p.end():]
    return tag, prefix, t.strip()


def _is_team_account(tag: str) -> bool:
    return bool(TEAM_ACCOUNT.search(tag) or team_of(tag))


def team_of(text: str) -> str | None:
    """Tri-code of the first NHL team named in ``text`` (nickname, short form or city)."""
    for nick, ab in EXTRA_ALIASES.items():
        if re.search(rf"\b{re.escape(nick)}\b", text):
            return ab
    hit = team_from_text(text)
    if hit:
        return hit
    folded = strip_accents(text)
    hits = [(m.start(), ab) for city, ab in CITIES.items() for m in [re.search(rf"\b{re.escape(city)}\b", folded)] if m]
    return sorted(hits)[0][1] if hits else None


def _verb(text: str) -> re.Match[str] | None:
    for m in VERB.finditer(text):
        if NEGATED_VERB.search(text[:m.start()]):
            continue
        return m
    return None


def classify_title(title: str) -> str:
    """"signing" | "rumour" | "other" for an r/hockey title."""
    tag, prefix, text = split_title(title)
    folded = strip_accents(text)
    if not CONTRACT_MENTION.search(text):
        return "other"
    if tag and (SCORE_TAG.search(tag) or NON_NEWS_TAG.match(tag)):
        return "other"
    if QUESTION.search(text) or QUOTE_LEAD.match(text) or NOT_SIGNING.search(text) or STAFF.search(text):
        return "other"
    if NON_NHL_CS.search(folded) or NON_NHL.search(folded) or (tag and (NON_NHL_CS.search(tag) or NON_NHL.search(tag))):
        return "other"
    if RUMOUR.search(text):
        return "rumour"
    m = _verb(text)
    if not m:
        return "other"
    if m["verb"].lower() == "signing" and not (prefix == "Report" or (tag and not _is_team_account(tag))):
        return "other"
    return "signing"


# --- names -----------------------------------------------------------------------------

def _tok(raw: str) -> str:
    tok = re.sub(r"'s$", "", raw.strip(",:;()\"'"))
    if not re.fullmatch(r"(?:[A-Z]\.)+", tok):
        tok = tok.rstrip(".")
    return tok


def _is_name_tok(tok: str) -> bool:
    low = tok.lower()
    return bool(NAME_TOKEN.match(tok)) and low not in NAME_STOP and low not in TEAM_WORDS \
        and not re.search(r"\d", tok) and not TERMISH.match(tok)


def _run_from(tokens: list[str]) -> list[str]:
    """Name tokens from the start of ``tokens`` (particles allowed inside), up to four."""
    run: list[str] = []
    for raw in tokens:
        tok = _tok(raw)
        if _is_name_tok(tok) or (run and tok.lower() in PARTICLES):
            run.append(tok)
            if raw.endswith((",", ":", ";", ")")) or len(run) == 4:
                break
            continue
        break
    while run and run[-1].lower() in PARTICLES:
        run.pop()
    return run


def _subject(before: str) -> list[str]:
    """The name run ending right before the verb ("Kirill Marchenko has agreed")."""
    tokens = before.split()
    run: list[str] = []
    for raw in reversed(tokens):
        tok = _tok(raw)
        if raw.endswith((",", ":", ";")) and run:
            break
        if _is_name_tok(tok) or (run and tok.lower() in PARTICLES):
            run.insert(0, tok)
            if len(run) == 4:
                break
            continue
        break
    while run and run[0].lower() in PARTICLES:
        run.pop(0)
    return run


def _object(after: str, verb: str) -> list[str]:
    """The name run after the verb ("sign Goaltender Casey DeSmith to", "agree to terms with Luke ...")."""
    if "terms" in verb.lower():
        m = re.match(r"\s*with\s+(.*)$", after, re.I)
        if not m:
            return []
        after = m.group(1)
    tokens = re.split(r"\s+", after.strip())
    i = 0
    while i < len(tokens) and (_tok(tokens[i]).lower() in SKIP_BEFORE_NAME or TERMISH.match(_tok(tokens[i]))
                               or re.fullmatch(r"\d+", _tok(tokens[i]))):
        i += 1
    return _run_from(tokens[i:])


def player_name(text: str, m: re.Match[str]) -> str:
    """The player named around the verb, as written."""
    head = re.split(r"\s+\|\s+|\s+\(", text, maxsplit=1)[0]
    if m.start() >= len(head):
        return ""
    subj = _subject(head[:m.start()])
    if subj:
        return " ".join(subj)
    return " ".join(_object(head[m.end():], m["verb"]))


# --- money and term -----------------------------------------------------------------------

def _money(text: str, flags: list[str]) -> tuple[int | None, int | None, int | None, int | None]:
    """(aav, cap hit, total, N from an "N x $A" form) as written in the title."""
    aav = cap = total = per_n = None
    for m in MONEY.finditer(text):
        value = money_value(m["num"], m["unit"])
        if value is None:
            continue
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + 40]
        x = PER_X_BEFORE.search(before)
        if x:
            field: str | None = "aav"
            per_n = per_n or int(x["n"])
        elif CAP_AFTER.match(after) or CAP_BEFORE.search(before):
            field = "cap"
        elif AAV_AFTER.match(after) or AAV_BEFORE.search(before):
            field = "aav"
        elif TOTAL_BEFORE.search(before) or TOTAL_AFTER.match(after):
            field = "total"
        else:
            field = None
        if field is None:
            flags.append("money_unlabeled")
            continue
        if field == "aav":
            if aav is None:
                aav = value
            elif aav != value:
                flags.append("multiple_amounts")
        elif field == "cap":
            if cap is None:
                cap = value
            elif cap != value:
                flags.append("multiple_amounts")
        elif total is None:
            total = value
        elif total != value:
            flags.append("multiple_amounts")
    return aav, cap, total, per_n


def _term(text: str, per_n: int | None, flags: list[str]) -> int | None:
    m = WORD_TERM.search(text)
    worded = NUMBER_WORDS[m["n"].lower()] if m else term_years(text)
    if worded and per_n and worded != per_n:
        flags.append("term_mismatch")
    term = worded or per_n
    if term is None and MULTI_YEAR.search(text):
        flags.append("multi_year_unspecified")
    return term


# --- seasons and clauses (only when literally stated) -------------------------------------

def _season_and_clause_rows(text: str, term: int | None, start: str | None, published: date, is_ext: bool,
                            is_elc: bool, cid: str, key: str, source_id: str, cap: int | None, aav: int | None,
                            flags: list[str]) -> tuple[list[SeasonClaim], list[ClauseClaim], str | None]:
    lines = [ln.strip() for ln in YR_LINE.split(text) if ln.strip()]
    seasons: list[SeasonClaim] = []
    rows: dict[int, dict[str, Any]] = {}
    if len(lines) > 1:
        rows, _first, _had = pp._year_rows(lines, is_elc, flags)
    clause_hits = pp._clauses(lines, term, flags) if pp.CLAUSE_WORD.search(text) else []
    if (rows or any(rng for _, _, rng, _ in clause_hits)) and start is None:
        start = pp._start_season(text, published, is_ext, flags)
    clauses: list[ClauseClaim] = []
    by_year: dict[int, list[str]] = {}
    for kind, n, rng, phrase in clause_hits:
        lo, hi = rng if rng else (None, None)
        clauses.append(ClauseClaim(
            claim_id=cid, contract_key=key, kind=kind,
            season_from=shift_season(start, lo - 1) if lo and start else None,
            season_to=shift_season(start, hi - 1) if hi and start else None,
            list_size=n, list_type=None, source_id=source_id, confidence_tier=TIER, evidence=phrase,
        ))
        if lo and hi:
            for y in range(lo, hi + 1):
                by_year.setdefault(y, []).append(f"{n} team {kind}" if n else kind)
    for y, r in sorted(rows.items()):
        seasons.append(SeasonClaim(
            claim_id=cid, contract_key=key, season=shift_season(start, y - 1), cap_hit_usd=cap, aav_usd=aav,
            base_salary_usd=r.get("base"), signing_bonus_usd=r.get("sb"), performance_bonus_usd=r.get("pb"),
            total_salary_usd=r["base"] + r["sb"] if r.get("base") is not None and r.get("sb") is not None else None,
            minors_salary_usd=r.get("minors"), one_way=r["one_way"],
            clause=r["clause"] or ("; ".join(by_year[y]) if y in by_year else None),
            source_id=source_id, confidence_tier=TIER, evidence=r["evidence"][:300],
        ))
    return seasons, clauses, start


# --- the post ------------------------------------------------------------------------------

def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


TRACKING = re.compile(r"^(?:utm_\w+|s|si|ref|ref_src|ref_url|share_id)$", re.I)


def ref_url(url: str) -> str:
    """The outbound link with tracking parameters (utm_*, ?s=20 ...) dropped."""
    parts = urlparse(url)
    query = "&".join(q for q in parts.query.split("&") if q and not TRACKING.match(q.split("=", 1)[0]))
    return parts._replace(query=query).geturl()


def post_id_of(post: dict[str, Any]) -> str:
    return str(post.get("id") or "").removeprefix("t3_")


def extract_post(post: dict[str, Any]) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], Source | None]:
    """One stored r/hockey post -> (claims, season rows, clause rows, source). Non-signings yield nothing."""
    nothing: tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], Source | None] = ([], [], [], None)
    title = post.get("title") or ""
    if classify_title(title) != "signing":
        return nothing
    tag, prefix, text = split_title(title)
    if tag and SKIP_TAG.search(tag):
        return nothing
    source_url = post.get("source_url")
    host = _host(source_url)
    if SKIP_DOMAIN.search(host) or NON_NHL_DOMAIN.search(host) or host.endswith(".ch"):
        return nothing
    if source_url and STAFF_URL.search(urlparse(source_url).path or ""):
        return nothing
    verb = _verb(text)
    if verb is None:
        return nothing

    flags: list[str] = []
    if tag:
        flags.append(f"insider:{tag}")
    if source_url:
        flags.append(f"ref:{ref_url(source_url)}")
    reporter = bool(tag) and not _is_team_account(tag or "")
    vword = verb["verb"].lower()
    if prefix == "Report" or (reporter and ("agree" in vword or vword == "signing")):
        flags.append("report")

    name = player_name(text, verb)
    if not name:
        return nothing
    if " " not in name:
        flags.append("surname_only")
    team = team_of(text)
    if team is None and tag:
        team = team_of(tag)
        if team:
            flags.append("team_from_tag")
    if team is None:
        return nothing

    aav, cap, total, per_n = _money(text, flags)
    term = _term(text, per_n, flags)
    is_pto = bool(PTO.search(text))
    if term is None and aav is None and cap is None and total is None and not is_pto:
        return nothing
    is_elc = bool(ELC.search(text))
    is_ext = bool(EXT.search(text))
    ctype = "PTO" if is_pto else "ELC" if is_elc else "extension" if is_ext else "standard"
    one_way = False if TWO_WAY.search(text) else True if ONE_WAY.search(text) else None
    if term and aav and total and abs(term * aav - total) > 0.02 * total:
        flags.append("total_vs_aav_mismatch")

    start = end = None
    th = THROUGH.search(text)
    if th:
        end = f"{th['y']}-{th['z']}"
        if term:
            start = shift_season(end, -(term - 1))
            flags.append("derived:start_from_end_and_term")
    else:
        fs = FOR_SEASON.search(text)
        if fs:
            start = f"{fs['y']}-{fs['z']}"
            end = shift_season(start, term - 1) if term else None

    created = post.get("created_at") or ""
    published = date.fromisoformat(created[:10])
    signed_at = published.isoformat()
    sid = f"reddit:{post.get('id')}"
    author = tag
    publisher = f"{PUBLISHER} quoting {tag}" if tag else PUBLISHER
    source = Source(
        source_id=sid, url=post.get("permalink") or "", publisher=publisher, author=author,
        published_at=created, confidence_tier=TIER, headline=_norm(title)[:200], kind=KIND,
    )
    cid = claim_id(sid, name, None)
    key = make_key(None, name, team, signed_at)
    seasons, clauses, start2 = _season_and_clause_rows(text, term, start, published, is_ext, is_elc, cid, key, sid,
                                                       cap, aav, flags)
    if start is None and start2 is not None:
        start = start2
        end = shift_season(start, term - 1) if term else end
    evidence = _norm(title)
    if source_url and source_url not in evidence:
        evidence = f"{evidence} / {source_url}"
    claim = Claim(
        claim_id=cid, contract_key=key, player_name=name, player_id=None, team=team, signed_at=signed_at,
        contract_type=ctype, term_years=term, aav_usd=aav, cap_hit_usd=cap, total_usd=total, nhl_salary_usd=None,
        minors_salary_usd=None, one_way=one_way, start_season=start, end_season=end,
        re_signed=bool(re.search(r"\bre-?sign", text, re.I)), source_id=sid, source_url=source.url,
        publisher=publisher, published_at=created, confidence_tier=TIER, extracted_by="regex",
        flags=list(dict.fromkeys(flags)), evidence=evidence, expiry_status=None, agent=None,
        signing_date_stated=False,
    )
    return [claim], seasons, clauses, source


def normalize_posts(posts: Iterable[dict[str, Any]]
                    ) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], list[Source]]:
    """Every stored post (``reddit_hockey.posts(store)``) -> claims, season rows, clause rows, sources."""
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
