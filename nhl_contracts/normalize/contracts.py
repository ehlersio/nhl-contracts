"""Regex-first extraction of contract facts from NHL.com stories and ESPN rows.

The output is a list of :class:`Claim`: one row per (source document, player
contract). A story that names two players yields two claims; the same signing
reported by a club release and an NHL.com staff story yields two claims with
the same ``contract_key``. Conflicts are kept, never resolved here.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date
from typing import Any

from ..config import STATIC_TEAMS
from ..store import Story
from . import llm_fallback
from .model import Claim, SeasonClaim, Source, claim_id, make_key
from .text import (
    MONEY,
    clean,
    money_value,
    name_key,
    season_from_date,
    shift_season,
    split_sentences,
    term_years,
)

# --- vocab -------------------------------------------------------------------

SIGN_VERB = re.compile(
    r"\b(re-?sign(?:s|ed|ing)?|sign(?:s|ed|ing)?|ink(?:s|ed)?|agree[sd]?\s+to\s+terms|"
    r"agree[sd]?\s+(?:on|to)\s+(?:a|an|the)\b|signing\s+of|joins?\b)",
    re.I,
)
CONTRACT_WORD = re.compile(
    r"\b(contracts?|deals?|extensions?|tryouts?|PTOs?|terms|contacts?|pacts?)\b", re.I
)
EXCLUDE = re.compile(
    r"\b(coach|coaches|coaching|scout|scouts|scouting|broadcast|radio|television|"
    r"partnership|sponsor|affiliat\w*|naming rights|jersey patch|"
    r"emergency backup|EBUG|(?:AHL|American Hockey League(?: \(AHL\))?|ECHL)(?:\s+\w+)?\s+contracts?|"
    r"amateur tryout|ATO|development camp|equipment manager|athletic trainer|announcer|"
    r"play-by-play|as (?:assistant |associate )?general manager|as president|"
    r"general manager (?:to|on) (?:a|an)|executive)\b",
    re.I,
)
# A sentence describing a previous contract rather than the new one.
PAST_DEAL = re.compile(
    r"\b(previous(?:ly)?|final (?:season|year) of|last (?:season|year) of|"
    r"(?:he|she|who) (?:had )?signed|was signed|originally signed|had agreed|"
    r"expir(?:ed|ing|es)|is in the (?:first|second|third|fourth|fifth|final))\b",
    re.I,
)
ELC = re.compile(r"\bentry[\s-]level\b", re.I)
EXT = re.compile(r"\b(extension|extends?|extended)\b", re.I)
PTO = re.compile(r"\b(professional tryout|PTO|tryout agreement|tryout contract)\b", re.I)
TWO_WAY = re.compile(r"\btwo[\s-]way\b", re.I)
ONE_WAY = re.compile(r"\bone[\s-]way\b", re.I)
MULTI_YEAR = re.compile(r"\bmulti-?year\b", re.I)
ARBITRATION = re.compile(r"\barbitration\b", re.I)

AAV_CTX = re.compile(
    r"(average annual value|annual average value|AAV|per season|per year|a season|a year|"
    r"annually|annual value|annual salary|annual cap hit|cap hit|salary cap hit|cap charge)",
    re.I,
)
CAP_HIT_CTX = re.compile(r"cap hit|cap charge", re.I)
SKIP_CTX = re.compile(
    r"(signing bonus|performance bonus|games?-played bonus|bonus|fine[ds]?\b|donat|raised|guarantee|"
    r"ticket|prize|grant|invest|awarded|buyout|retain|escrow|salary cap (?:ceiling|floor|"
    r"space|limit|is|of)|cap space|upper limit)",
    re.I,
)
MINORS_CTX = re.compile(r"(American Hockey League|AHL|minor[\s-]league)", re.I)
NHL_LEVEL_CTX = re.compile(r"\bNHL level\b|\bNHL salary\b|\bin the NHL\b", re.I)
TOTAL_CTX = re.compile(
    r"(contract|deal|extension|agreement|worth|valued|value of|total(?:ing|s)?|over the|"
    r"\bpay\b|\bpaid\b|\bearn\b|salary of)", re.I
)

# Lowercase filler between the verb and the name: "recently acquired forward",
# "2024 third-round draft pick", "free-agent forward". Names are capitalised, so
# the patterns are case-sensitive except for their verbs.
FILLER = (r"(?:[A-Z][a-z]+\s+(?=(?:forward|defen|goal|cent|wing|free|veteran|prospect|rookie)))?"
          r"(?:[a-z0-9][\w'\-]*\s+){0,6}")
POS_WORDS = re.compile(
    r"^(?:(?:left|right)[\s-]wing(?:er)?s?|forwards?|defen[cs]e(?:man|men)|goal(?:tender|ie)s?|"
    r"cent(?:er|re)s?|wingers?|captain|veteran|rookie|prospect|free[\s-]agent|"
    r"restricted|unrestricted|newly|recently|acquired|signed|team|the|its|their|both|and|"
    r"F|D|G|C|LW|RW|LD|RD|Fs|Ds|Gs|Cs)\s+",
    re.I,
)
NAME_TOKEN = r"[A-ZÀ-ſ][\w'.\-À-ſ]*"
PARTICLE = r"(?:(?:de|van|der|von|da|di|du|la|le|st\.)\s+)?"
NAME = (
    r"(?P<name>" + NAME_TOKEN + r"(?:\s+" + PARTICLE + NAME_TOKEN + r"){0,3})"
)
PAREN = r"(?:\s*\((?P<paren>[^)]*)\))?"
DEAL_WORD = r"(?:contract|deal|extension|tryout|PTO|agreement|contact|pact)s?"
# A blob of one or more names: "Arthur Kaliyev, Wyatt Bongiovanni and goaltender Hunter Shepard"
NAMES_BLOB = (
    r"(?P<names>" + NAME_TOKEN + r"(?:\s*\([^)]*\))?"
    r"(?:(?:\s+" + PARTICLE + NAME_TOKEN + r"|,\s*" + FILLER + NAME_TOKEN
    + r"|,?\s+(?:and|along\s+with|as\s+well\s+as)\s+" + FILLER + NAME_TOKEN + r")"
    r"(?:\s*\([^)]*\))?)*)"
)

# "signed forward Marat Khusnutdinov to a three-year contract extension"
P_SIGN_TO = re.compile(
    r"\b(?i:re-?signed|signed|signs|re-?signs|inked|inks|signing\s+of|re-?signing\s+of)\s+"
    + FILLER + NAMES_BLOB +
    r"\s+(?:both\s+|all\s+)?(?i:to|on)\s+(?:a|an|the|his|new)?\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*)",
)
# "agreed to terms with forward Pavel Dorofeyev on a seven-year contract"
P_TERMS_WITH = re.compile(
    r"\b(?i:agree[sd]?\s+to\s+terms\s+with)\s+" + FILLER + NAMES_BLOB +
    r"\s+(?:both\s+|all\s+)?(?i:on|to)\s+(?:a|an|the)?\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*)",
)
# "agreed to terms on a one-year, two-way contract with forward Brett Berard"
P_TERMS_ON = re.compile(
    r"\b(?i:agree[sd]?\s+to\s+terms\s+on)\s+(?:a|an|the)?\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*?)"
    r"\s+with\s+" + FILLER + NAMES_BLOB + r"(?=[.,;]|\s|$)",
)
# "Philip Broberg signed a six-year, $48 million contract with the St. Louis Blues"
P_NAME_SIGNED = re.compile(
    r"(?:^|[.;:]\s+|\s)" + NAME + r"(?:,\s+who\s+[^,]+,|,\s*\d{2}(?:\s*\([^)]*\))?,)?\s+(?:has\s+|have\s+|had\s+)?"
    r"(?:re-?signed|signed|inked|agreed\s+to)\s+(?:a|an|the|his)?\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*)",
)
# "Hischier ... agreed to terms on a five-year contract extension" (name before verb)
P_NAME_TERMS = re.compile(
    r"(?:^|[.;:]\s+|\s)" + NAME + r"(?:,\s*\d{2}(?:\s*\([^)]*\))?,)?\s+(?:has\s+)?agreed\s+to\s+terms\s+"
    r"(?:with\s+the\s+[A-Z][\w .]+?\s+)?on\s+"
    r"(?:a|an|the)?\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*)",
)
PLAYER_LINK = re.compile(r"nhl\.com/player/(?:[a-z\-]+-)?(\d{7})", re.I)
FORGE_PLAYER = re.compile(r'<forge-entity[^>]*slug="([a-z\-]+)-(\d{7})"[^>]*code="player"[^>]*>([^<]+)</forge-entity>')
FORGE_PLAYER2 = re.compile(r'<forge-entity[^>]*code="player"[^>]*slug="([a-z\-]+)-(\d{7})"[^>]*>([^<]+)</forge-entity>')

TEAM_NICKNAMES: dict[str, str] = {
    "NJD": "Devils", "NYI": "Islanders", "NYR": "Rangers", "PHI": "Flyers", "PIT": "Penguins",
    "BOS": "Bruins", "BUF": "Sabres", "MTL": "Canadiens", "OTT": "Senators", "TOR": "Maple Leafs",
    "CAR": "Hurricanes", "FLA": "Panthers", "TBL": "Lightning", "WSH": "Capitals",
    "CHI": "Blackhawks", "DET": "Red Wings", "NSH": "Predators", "STL": "Blues", "CGY": "Flames",
    "COL": "Avalanche", "EDM": "Oilers", "VAN": "Canucks", "ANA": "Ducks", "DAL": "Stars",
    "LAK": "Kings", "SJS": "Sharks", "CBJ": "Blue Jackets", "MIN": "Wild", "WPG": "Jets",
    "VGK": "Golden Knights", "SEA": "Kraken", "UTA": "Mammoth",
}
TEAM_ALIASES: dict[str, str] = {"Canes": "CAR", "Utah": "UTA", "Utah Hockey Club": "UTA",
                                "Habs": "MTL", "Isles": "NYI", "Leafs": "TOR", "Bolts": "TBL",
                                "Preds": "NSH", "Pens": "PIT", "Knights": "VGK", "Jackets": "CBJ",
                                "Caps": "WSH", "Hawks": "CHI", "Wings": "DET", "Avs": "COL"}
ESPN_ABBREV = {"NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "WSH": "WSH", "MTL": "MTL"}


# --- model: see model.py ---

# --- helpers -----------------------------------------------------------------

def tier_for(story: Story) -> tuple[str, str]:
    """Return (tier, publisher)."""
    tag = story.author_tag or ""
    if tag == "nhl-created":
        return "B", "NHL.com"
    if tag.endswith("-created"):
        return "A", f"{tag[:-8].upper()} (club release on NHL.com)"
    return "C", "NHL.com"


def team_from_tags(tags: list[str], teams: dict[int, str]) -> list[str]:
    out = []
    for t in tags:
        if t.startswith("teamid-"):
            try:
                ab = teams.get(int(t[7:]))
            except ValueError:
                ab = None
            if ab and ab not in out:
                out.append(ab)
    return out


def team_from_text(text: str) -> str | None:
    for nick, ab in TEAM_ALIASES.items():
        if re.search(rf"\b{re.escape(nick)}\b", text):
            return ab
    hits = [(m.start(), ab) for ab, nick in TEAM_NICKNAMES.items()
            for m in [re.search(rf"\b{re.escape(nick)}\b", text)] if m]
    if hits:
        return sorted(hits)[0][1]
    return None


# A figure tied to one season of a multi-season schedule is a season amount, never
# the contract's total or AAV: "$850,000 in 2026-27, $900,000 in 2027-28",
# "$850,000 for the first year", "the second year as a one-way $900,000 contract",
# and every figure after a "2027-28:" label when the text has two or more labels.
ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|final|last)"
SEASON_AFTER = re.compile(r"\s*(?:in|for)\s+(?:the\s+)?20\d\d-\d\d\b")
ORDINAL_AFTER = re.compile(r"\s*for\s+the\s+" + ORDINAL + r"\b")
ORDINAL_BEFORE = re.compile(ORDINAL + r"\s+(?:year|season)\b[^$]{0,40}?\b(?:one|two)[\s-]way\s*$", re.I)
SEASON_LABEL = re.compile(r"\b(20\d\d-\d\d)\s*:\s*")
NHL_LEVEL_LEAD = re.compile(r"\b(?:on|at)\s+the\s+NHL\s+level\b", re.I)
# "$1 million in salary": a base salary, not a total
SALARY_AFTER = re.compile(r"\s*in\s+(?:base\s+)?salary\b", re.I)
# a context phrase that introduces the next figure ("$9.5M, with an AAV of $4.75M")
INTRODUCES_NEXT = re.compile(
    r"(?:average annual value|annual average value|AAV|cap hit|per season|per year|annual value|annual salary)"
    r"(?:\s*\(AAV\))?(?:\s+(?:in|at)\s+the\s+NHL)?\s*(?:of|is|at|:|=)?\s*$",
    re.I,
)


def _season_figures(text: str, matches: list[re.Match[str]]) -> set[int]:
    """Indexes of the figures in ``text`` that are amounts for one season of a schedule."""
    out: set[int] = set()
    linked = [i for i, m in enumerate(matches) if SEASON_AFTER.match(text, m.end())]
    if len(linked) >= 2:
        out.update(linked)
    labels = list(SEASON_LABEL.finditer(text))
    if len(labels) >= 2:
        out.update(i for i, m in enumerate(matches) if m.start() > labels[0].start())
    for i, m in enumerate(matches):
        if ORDINAL_AFTER.match(text, m.end()) or ORDINAL_BEFORE.search(text[max(0, m.start() - 90):m.start()]):
            out.add(i)
    return out


def classify_money(text: str) -> dict[str, int | None]:
    """Assign every dollar figure in ``text`` to total / aav / cap hit / salaries."""
    out: dict[str, int | None] = {"total": None, "aav": None, "cap_hit": None,
                                  "nhl_salary": None, "minors_salary": None}
    matches = list(MONEY.finditer(text))
    per_season = _season_figures(text, matches)
    for i, m in enumerate(matches):
        value = money_value(m.group("num"), m.group("unit"))
        before = text[max(0, m.start() - 70):m.start()]
        after = text[m.end():m.end() + 45]
        if i + 1 < len(matches) and matches[i + 1].start() < m.end() + 45:
            # context after a figure stops at the next figure, minus any phrase that introduces it
            after = text[m.end():matches[i + 1].start()]
            after = after[:len(after) - len(m2.group(0))] if (m2 := INTRODUCES_NEXT.search(after)) else after
        if value is None and not m.group("unit"):
            raw = float(m.group("num").replace(",", ""))
            if raw < 100 and re.match(r"\s*(AAV|million|M\b|average annual|cap hit)", after, re.I):
                value = int(round(raw * 1_000_000))
        if value is None:
            continue
        near_before = before[-40:]
        # "$1.025 million/$85,000" two-way pair
        if after.startswith("/") and i + 1 < len(matches) and matches[i + 1].start() == m.end() + 1:
            out["nhl_salary"] = out["nhl_salary"] or value
            continue
        if before.endswith("/"):
            out["minors_salary"] = out["minors_salary"] or value
            continue
        if SKIP_CTX.search(near_before) or SKIP_CTX.search(after[:30]):
            continue
        is_minors = bool(MINORS_CTX.search(after[:40])
                         or re.search(r"(AHL|American Hockey League)\s+(level|salary)\s*(?:of|is|at)?\s*$", before))
        is_nhl = bool(NHL_LEVEL_CTX.search(after[:25])
                      or re.search(r"NHL\s+(level|salary)\s*(?:of|is|at)?\s*$", before))
        is_aav = bool(AAV_CTX.search(near_before) or AAV_CTX.search(after[:30]))
        if i in per_season:
            # a season amount: at most the NHL or minors half of that season, never total or AAV
            if is_minors:
                out["minors_salary"] = out["minors_salary"] or value
            elif is_nhl or NHL_LEVEL_LEAD.search(text[:m.start()]):
                out["nhl_salary"] = out["nhl_salary"] or value
            continue
        if SALARY_AFTER.match(after):
            continue
        if is_minors and not is_aav:
            out["minors_salary"] = out["minors_salary"] or value
            continue
        if is_aav:
            key = "cap_hit" if (CAP_HIT_CTX.search(near_before) or CAP_HIT_CTX.search(after[:30])) else "aav"
            if out[key] is None:
                out[key] = value
            if is_nhl and out["nhl_salary"] is None:
                out["nhl_salary"] = value
            continue
        if is_nhl:
            out["nhl_salary"] = out["nhl_salary"] or value
            continue
        if TOTAL_CTX.search(near_before) or TOTAL_CTX.search(after[:25]) or re.search(r"year,?\s*$", before):
            if out["total"] is None:
                out["total"] = value
    return out


def contract_type(text: str, headline: str) -> str:
    t = f"{headline} {text}"
    if PTO.search(t):
        return "PTO"
    if ELC.search(t):
        return "ELC"
    if EXT.search(t):
        return "extension"
    return "standard"


def seasons_from(text: str, published: date, term: int | None, is_extension: bool
                 ) -> tuple[str | None, str | None, list[str]]:
    flags: list[str] = []
    start = end = None
    m = re.search(r"through (?:the )?(?:end of the )?(20\d\d-\d\d)", text)
    if m:
        end = m.group(1)
    m = re.search(r"\(?(20\d\d-\d\d)\s+(?:to|through|-)\s+(20\d\d-\d\d)\)?", text)
    if m:
        start, end = m.group(1), m.group(2)
    m = re.search(r"(?:begin(?:s|ning)?|start(?:s|ing)?|commenc(?:es|ing)|set to begin|set to start)"
                  r"\s+(?:with|in|during)\s+(?:the\s+)?(20\d\d-\d\d)", text)
    if m:
        start = m.group(1)
    elif re.search(r"\b(?:begins?|starts?|starting|beginning) next season\b", text):
        start = shift_season(season_from_date(published), 1)
    elif term == 1 and re.search(r"\bfor the (20\d\d-\d\d) season\b", text):
        start = end = re.search(r"\bfor the (20\d\d-\d\d) season\b", text).group(1)
    elif term == 1 and re.search(r"\((20\d\d-\d\d)\)", text):
        start = end = re.search(r"\((20\d\d-\d\d)\)", text).group(1)
    if term and start and not end:
        end = shift_season(start, term - 1)
    elif term and end and not start:
        start = shift_season(end, -(term - 1))
    if start and end and term and shift_season(start, term - 1) != end:
        flags.append("season_span_mismatch")
    return start, end, flags


def consistency_flags(term: int | None, aav: int | None, total: int | None) -> list[str]:
    if term and aav and total and term > 1:
        if abs(total - term * aav) > max(0.02 * total, 60_000):
            return ["total_vs_aav_mismatch"]
    return []


LEAGUE_MIN = [("2026-07-01", 850_000), ("2025-07-01", 775_000), ("2023-07-01", 775_000),
              ("2021-07-01", 750_000), ("2019-07-01", 700_000), ("2017-07-01", 650_000)]


def minimum_flags(signed_at: str, *amounts: int | None) -> list[str]:
    floor = next((m for d, m in LEAGUE_MIN if signed_at >= d), 0)
    for a in amounts:
        if a is not None and a < floor:
            return ["below_league_minimum"]
    return []


def _title_name(name: str) -> str:
    """Trim position words and punctuation; fix ALL-CAPS club headlines ("ADAM NOVOTNÝ")."""
    name = name.strip().strip(",;:")
    while True:
        stripped = POS_WORDS.sub("", name)
        if stripped == name:
            break
        name = stripped
    parts = name.split()
    if parts and parts[-1].endswith(".") and len(parts[-1]) > 3:
        parts[-1] = parts[-1][:-1]
    name = " ".join(parts)
    if name.isupper():
        return " ".join(w.capitalize() if "-" not in w else "-".join(p.capitalize() for p in w.split("-"))
                        for w in name.split())
    return name


def _expand_name(name: str, texts: list[str], players: dict[str, int]) -> str:
    """'signed Melanson to ...' -> 'Jacob Melanson' when the story names him in full."""
    if len(name.split()) != 1:
        return name
    for full in players:
        if full.split()[-1] == name:
            return full
    for text in texts:
        m = re.search(r"((?:[A-Z\u00c0-\u017f][\w'.\-\u00c0-\u017f]+\s+){1,2})" + re.escape(name) + r"\b", text)
        if m:
            cand = _title_name(m.group(0))
            if _valid_name(cand) and not any(t.lower() in NAME_STOP for t in cand.split()):
                return cand
    return name


NAME_STOP = {"sign", "signs", "signed", "re-sign", "re-signs", "re-signed", "with", "to", "on",
             "agree", "agrees", "agreed", "terms", "and", "the", "inks", "release", "today"}


# --- forge stories -----------------------------------------------------------

def is_signing_story(story: Story, lead: str) -> bool:
    """True when the headline, title or lead has a sentence that signs a contract
    and is not about a coach, executive, broadcaster or affiliate."""
    # The headline decides when it can: "Green signs contract as Senators coach" is
    # a coach story even though its lead never says "coach".
    head_clauses = [c for text in (story.headline, story.title)
                    for c in re.split(r"[;|]", clean(text)) if SIGN_VERB.search(c)]
    if head_clauses and all(EXCLUDE.search(c) for c in head_clauses):
        return False
    for text in (story.headline, story.title, lead):
        for chunk in re.split(r"[;|]", clean(text)):
            for sent in split_sentences(chunk):
                if SIGN_VERB.search(sent) and CONTRACT_WORD.search(sent) and not EXCLUDE.search(sent):
                    return True
    return False


def forge_players(body: str) -> dict[str, int]:
    """Player name -> NHL id from forge-entity markup or nhl.com/player links."""
    out: dict[str, int] = {}
    for rx in (FORGE_PLAYER, FORGE_PLAYER2):
        for _slug, pid, name in rx.findall(body or ""):
            out.setdefault(name.strip(), int(pid))
    for m in re.finditer(r"\[([^\]]+)\]\(https?://www\.nhl\.com/player/(?:[a-z\-]+-)?(\d{7})\)", body or ""):
        out.setdefault(clean(m.group(1)).strip(), int(m.group(2)))
    return out


def _split_names(blob: str) -> list[tuple[str, str | None]]:
    """'A ($1M AAV), B and goaltender C' -> [(A, '$1M AAV'), (B, None), (C, None)]."""
    out = []
    for part in re.split(r",\s*(?![^(]*\))|\s+(?:and|along\s+with|as\s+well\s+as)\s+(?![^(]*\))", blob):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(?P<name>[^()]+?)\s*(?:\((?P<paren>[^)]*)\))?$", part)
        if not m:
            continue
        out.append((m.group("name").strip(), m.group("paren")))
    return out


def _segments(sentence: str) -> list[tuple[str, str, str | None]]:
    """Find (name, deal phrase, parenthetical) tuples in one sentence."""
    found: list[tuple[str, str, str | None]] = []
    for rx in (P_SIGN_TO, P_TERMS_WITH, P_TERMS_ON, P_NAME_TERMS, P_NAME_SIGNED):
        for m in rx.finditer(sentence):
            deal = m.group("deal").strip()
            gd = m.groupdict()
            if "names" in gd and gd["names"]:
                for name, paren in _split_names(gd["names"]):
                    found.append((name, deal, paren))
            else:
                found.append((gd["name"].strip(), deal, gd.get("paren")))
        if found:
            break
    # "signed X to a ..., and forward Y to a ... contract" inside one sentence
    if len(found) == 1:
        rest = sentence[sentence.find(found[0][1]) + len(found[0][1]):]
        for mm in re.finditer(r",?\s+and\s+" + FILLER + NAME + PAREN +
                              r"\s+to\s+(?:a|an)\s*(?P<deal>.*?" + DEAL_WORD + r"\b[^;]*)", rest):
            found.append((mm.group("name").strip(), mm.group("deal").strip(), mm.group("paren")))
    return found


BAD_NAMES = re.compile(
    r"^(The|Today|General|President|Hockey|Club|Team|National|Vice|Senior|Executive|Monday|"
    r"Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|May|June|"
    r"July|August|September|October|November|December|It|He|She|They|This|Financial)\b",
)


def _valid_name(name: str) -> bool:
    if BAD_NAMES.match(name):
        return False
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    if any(p.lower() in TEAM_NICKNAMES_LOWER for p in parts):
        return False
    return True


TEAM_NICKNAMES_LOWER = {n.lower() for n in TEAM_NICKNAMES.values()} | {"hockey", "club", "nhl"}


def extract_story(story: Story, teams: dict[int, str]) -> tuple[list[Claim], Source | None]:
    body_clean = clean(story.body)
    paragraphs = [p for p in body_clean.split("\n") if p.strip()]
    lead = paragraphs[0] if paragraphs else clean(story.summary)
    if not is_signing_story(story, lead):
        return [], None
    tier, publisher = tier_for(story)
    published = story.content_date[:10]
    pub_date = date.fromisoformat(published) if published else date.today()
    source = Source(
        source_id=f"forge:{story.slug}", url=story.url, publisher=publisher,
        author=story.created_by, published_at=story.content_date, confidence_tier=tier,
        headline=story.headline, kind="nhl_forge",
    )
    headline = clean(story.headline)
    players = forge_players(story.body)
    tag_ids = [int(t[9:]) for t in story.tags if t.startswith("playerid-") and t[9:].isdigit()]
    team_tags = team_from_tags(story.tags, teams)
    author_team = None
    if story.author_tag and story.author_tag != "nhl-created":
        author_team = story.author_tag[:-8].upper()
        if author_team not in TEAM_NICKNAMES:
            author_team = None
    if author_team:
        team = author_team
    elif len(team_tags) == 1:
        team = team_tags[0]
    else:
        team = team_from_text(headline) or (team_tags[0] if team_tags else None)

    # Candidate sentences: headline first (cheap and formulaic), then paragraphs.
    texts = [headline] + paragraphs
    claims: dict[str, Claim] = {}
    for pi, text in enumerate(texts):
        sentences = split_sentences(text)
        signing_idx = [i for i, se in enumerate(sentences)
                       if SIGN_VERB.search(se) and CONTRACT_WORD.search(se)]
        for si, sent in enumerate(sentences):
            if si not in signing_idx:
                continue
            if pi > 0 and PAST_DEAL.search(sent):
                continue
            if EXCLUDE.search(sent):
                continue
            segs = _segments(sent)
            if not segs:
                continue
            window = sent
            if pi > 0:
                nxt_idx = next((i for i in signing_idx if i > si), len(sentences))
                window = " ".join(sentences[si:nxt_idx])
                nxt = texts[pi + 1] if pi + 1 < len(texts) else ""
                if nxt_idx == len(sentences) and re.match(
                    r"(The (contract|deal|extension|agreement|two|three|four|five|six|seven|eight|one)"
                    r"|It\b|Financial|Per club)", nxt
                ):
                    window = f"{window} {nxt}"
            for name, deal, paren in segs:
                name = _expand_name(_title_name(name), texts, players)
                if not _valid_name(name):
                    continue
                key = name_key(name)
                ctype = contract_type(deal, headline if pi == 0 else "")
                if pi > 0 and ctype == "standard" and EXT.search(headline):
                    ctype = "extension"
                term = term_years(deal) or term_years(sent)
                money = classify_money(paren or "") if paren else {}
                if len(segs) == 1:
                    m2 = classify_money(window)
                    for k, v in m2.items():
                        if money.get(k) is None:
                            money[k] = v
                    if not any(money.get(k) for k in ("aav", "cap_hit", "total", "nhl_salary")):
                        lab = re.search(r"\b(?:AAV|Cap Hit|Average Annual Value)\s*:\s*(\$\s?[\d,.]+\s*(?:million|M)?)",
                                        body_clean, re.I)
                        if lab:
                            money["aav"] = classify_money("AAV of " + lab.group(1)).get("aav")
                        if term is None:
                            lab_t = re.search(r"\bTERM\s*:\s*([^\n]{0,60})", body_clean, re.I)
                            if lab_t:
                                term = term_years(lab_t.group(1))
                else:
                    m2 = classify_money(deal)
                    for k, v in m2.items():
                        if money.get(k) is None:
                            money[k] = v
                one_way = True if ONE_WAY.search(deal) else (False if TWO_WAY.search(deal) else None)
                if one_way is None and len(segs) == 1:
                    one_way = True if ONE_WAY.search(window) else (False if TWO_WAY.search(window) else None)
                flags: list[str] = []
                if MULTI_YEAR.search(deal) and not term:
                    flags.append("multi_year_unspecified")
                if ARBITRATION.search(window):
                    flags.append("arbitration")
                if len(segs) > 1:
                    flags.append("multi_player_story")
                if re.search(r"\baccording to\b|\bsources?\b|\breported\b", sent, re.I):
                    flags.append("attributed_report")
                if re.search(r"\b20\d\d-\d\d\s*:\s*\$|\b(?:first|second|third|final) (?:year|season) (?:of|as|at)\b"
                             r"|signing bonus|base salary|\$[\d,.]+(?:\s*million)?\s+in\s+20\d\d-\d\d"
                             r"|for the (?:first|second) (?:year|season)\b", window):
                    flags.append("per_season_breakdown")
                aav, cap_hit, total = money.get("aav"), money.get("cap_hit"), money.get("total")
                nhl_sal, min_sal = money.get("nhl_salary"), money.get("minors_salary")
                if term == 1:
                    if aav is None and cap_hit is None and total is None and nhl_sal:
                        aav = nhl_sal
                        flags.append("derived:aav_from_nhl_salary")
                    if total is None and (aav or cap_hit):
                        total = aav or cap_hit
                        flags.append("derived:total_one_year")
                    if aav is None and total:
                        aav = total
                        flags.append("derived:aav_one_year")
                flags += consistency_flags(term, aav or cap_hit, total)
                flags += minimum_flags(published, aav, cap_hit, nhl_sal)
                start, end, sflags = seasons_from(window, pub_date, term,
                                                  ctype == "extension")
                flags += sflags
                pid = players.get(name)
                if pid is None:
                    for pname, ppid in players.items():
                        if name_key(pname) == key or name_key(pname).split()[-1] == key.split()[-1]:
                            pid = ppid
                            break
                re_signed = bool(re.search(r"\bre-?sign", sent, re.I))
                claim = Claim(
                    claim_id=claim_id(source.source_id, name, pid),
                    contract_key=make_key(pid, name, team, published),
                    player_name=name, player_id=pid, team=team, signed_at=published,
                    contract_type=ctype, term_years=term, aav_usd=aav, cap_hit_usd=cap_hit,
                    total_usd=total, nhl_salary_usd=nhl_sal, minors_salary_usd=min_sal,
                    one_way=one_way, start_season=start, end_season=end, re_signed=re_signed,
                    source_id=source.source_id, source_url=source.url, publisher=publisher,
                    published_at=story.content_date, confidence_tier=tier, extracted_by="regex",
                    flags=flags, evidence=sent[:400],
                )
                prev = claims.get(key)
                if prev is None:
                    claims[key] = claim
                else:
                    _merge_into(prev, claim)
    out = list(claims.values())
    if len(out) == 1 and len(tag_ids) == 1 and out[0].player_id is None:
        out[0].player_id = tag_ids[0]
        out[0].contract_key = make_key(tag_ids[0], out[0].player_name, out[0].team, published)
        out[0].claim_id = claim_id(source.source_id, out[0].player_name, tag_ids[0])
    needs_llm = not out or all(c.term_years is None and c.aav_usd is None and c.total_usd is None for c in out) or any(
        "per_season_breakdown" in c.flags and c.aav_usd is None and c.cap_hit_usd is None for c in out)
    if needs_llm:
        llm = llm_fallback.extract(story)
        if llm:
            out = [Claim(**{**asdict(c), **{k: v for k, v in d.items() if k in Claim.__dataclass_fields__},
                           "extracted_by": "llm"}) for c, d in zip(out or [out[0]] if out else [], llm, strict=False)]
        elif not out:
            return [], source
    return out, source


def _merge_into(base: Claim, new: Claim) -> None:
    """Fill gaps in ``base`` from a later sentence about the same player in the same story."""
    for f in ("term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd",
              "minors_salary_usd", "one_way", "start_season", "end_season", "player_id"):
        if getattr(base, f) is None and getattr(new, f) is not None:
            setattr(base, f, getattr(new, f))
    if base.contract_type == "standard" and new.contract_type != "standard":
        base.contract_type = new.contract_type
    for fl in new.flags:
        if fl not in base.flags and not fl.startswith("derived:"):
            base.flags.append(fl)
    if base.term_years == 1:
        if base.total_usd is None and base.aav_usd:
            base.total_usd = base.aav_usd
        if base.aav_usd is None and base.total_usd:
            base.aav_usd = base.total_usd
    base.flags = [f for f in base.flags if f != "total_vs_aav_mismatch"]
    base.flags += consistency_flags(base.term_years, base.aav_usd or base.cap_hit_usd, base.total_usd)
    base.contract_key = make_key(base.player_id, base.player_name, base.team, base.signed_at)
    base.claim_id = claim_id(base.source_id, base.player_name, base.player_id)


# --- per-season structure ----------------------------------------------------
#
# Club releases sometimes give the year-by-year structure of a deal. These rows
# are read only from what a sentence states about a season: a "2027-28: $X"
# schedule, "$X in 2026-27", "$X for the first year", "the second year as a
# one-way $X contract", or, for a one-year deal, a base / bonus / NHL-AHL split.
# A claim with only an AAV and a term gets no season rows.

ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5, "seventh": 6, "eighth": 7}
BARE_AMOUNT = re.compile(r"(?<![\d$.,])\d{1,3}(?:,\d{3}){1,3}(?![\d,])")
SEASON_LINK = re.compile(r"\b(?:in|for)\s+(?:the\s+)?(20\d\d-\d\d)(?:\s+(?:NHL\s+)?season)?\b")
AS_FOLLOWS = re.compile(
    r"\bfor\s+(?:the\s+)?(20\d\d-\d\d)(?:\s+season)?\s+(?:are|is|will be)\s+as\s+follows\s*:\s*(.+)$", re.I
)
GUARANTEE_BEFORE = re.compile(r"guarantee(?:d)?(?:\s+of)?(?:\s+at\s+least)?\s*$")
MINORS_BEFORE = re.compile(r"(?:AHL|American Hockey League)\s+(?:level|salary)\s*(?:of|is|at)?\s*$")
# "bonuses that could reach a maximum value of $5 million": the bonus cap or the deal's maximum?
BONUS_UNCLEAR_BEFORE = re.compile(r"\bbonus(?:es)?\s+(?:that\s+)?(?:could|can|may|might|worth|of up to|up to)\b[^$]*$")
ORDINAL_AMOUNT = re.compile(
    r"(?P<amt>\$\s?\d[\d,]*(?:\.\d+)?(?:\s*million)?)\s+for\s+the\s+(?P<ord>" + ORDINAL + r")(?:\s+(?:year|season))?\b",
    re.I,
)
ORDINAL_AS = re.compile(
    r"\bthe\s+(?P<ord>" + ORDINAL + r")\s+(?:year|season)\b(?:\s+of\s+the\s+[\w-]+\s+(?:contract|deal))?\s+as\s+an?\s+"
    r"(?P<way>one|two)[\s-]way(?:\s+(?P<amt>\$\s?\d[\d,]*(?:\.\d+)?(?:\s*million)?))?",
    re.I,
)
ONE_YEAR_SEASON = re.compile(r"\bfor\s+(?:the\s+)?(20\d\d-\d\d)(?:\s+NHL)?\s+season\b", re.I)
SALARY_ROLES = {"base", "nhl", "minors", "minors_each", "signing", "perf"}


def _amounts(text: str, bare: bool = False) -> list[tuple[int, int, int]]:
    """(value, start, end) for every figure; ``bare`` also reads '4,100,000' written without '$'."""
    out = []
    for m in MONEY.finditer(text):
        v = money_value(m.group("num"), m.group("unit"))
        if v is not None:
            out.append((v, m.start(), m.end()))
    if bare:
        taken = [(s, e) for _, s, e in out]
        for m in BARE_AMOUNT.finditer(text):
            if not any(s <= m.start() < e for s, e in taken):
                v = money_value(m.group(0), None)
                if v is not None and v >= 100_000:
                    out.append((v, m.start(), m.end()))
    return sorted(out, key=lambda t: t[1])


def _role(text: str, start: int, end: int, prev_end: int, next_start: int | None) -> str:
    """What one figure is, from the words around it (context stops at the neighbouring figures)."""
    before = text[max(prev_end, start - 60):start]
    after = text[end:next_start if next_start is not None else end + 60][:60]
    if after.startswith("/") and next_start == end + 1:
        return "nhl"
    if before.endswith("/"):
        return "minors"
    if re.match(r"\s*\)?\s*guaranteed\b", after) or GUARANTEE_BEFORE.search(before):
        return "guaranteed"
    if re.search(r"signing\s+bonus(?:es)?\s+total(?:ing|s)?\s*$", before) or re.match(
            r"\s*in\s+signing\s+bonus(?:es)?\s+over\b", after):
        return "signing_total"
    if re.match(r"\s*(?:in\s+)?signing\s+bonus", after) or re.search(r"signing\s+bonus(?:\s+of)?\s*$", before):
        return "signing"
    if re.match(r"\s*(?:in\s+)?(?:(?:additional|potential)\s+)*(?:performance|games?[\s-]played|achievement)\s+bonus",
                after, re.I):
        return "perf"
    if BONUS_UNCLEAR_BEFORE.search(before):
        return "bonus_unclear"
    if SALARY_AFTER.match(after) or re.search(r"\bbase(?:\s+salary)?(?:\s+(?:of|is|at))?\s*$", before):
        return "base"
    if MINORS_CTX.search(after[:45]) or MINORS_BEFORE.search(before):
        return "minors_each" if re.match(r"\s*(?:per|a|each)\s+(?:season|year)\b", after) else "minors"
    if (re.match(r"\s*(?:at|on|in)\s+the\s+NHL\b", after) or NHL_LEVEL_CTX.search(after[:25])
            or re.search(r"NHL\s+(?:level|salary)\s*(?:of|is|at)?\s*$", before)):
        return "nhl"
    if AAV_CTX.search(before[-40:]) or AAV_CTX.search(after[:30]):
        return "aav"
    if re.match(r"\s*(?:contract|deal|extension|agreement)\b", after) or SKIP_CTX.search(before[-40:]):
        return "other"
    return "plain"


def _read_segment(text: str, *, bare: bool = False, nhl_lead: bool = False, labelled: bool = False
                  ) -> tuple[dict[str, Any], list[str]]:
    """Read one season's words into salary fields, plus claim flags (guarantees, contract-long totals).

    ``bare``: also read amounts written without '$'. ``nhl_lead``: the sentence opened
    with "On the NHL level", so unlabelled figures are NHL salaries. ``labelled``: the
    text is one season's slot in a schedule, so "one-way deal at $X" is that season's salary."""
    vals: dict[str, Any] = {}
    flags: list[str] = []
    amounts = _amounts(text, bare)
    roles = [_role(text, s, e, amounts[i - 1][2] if i else 0, amounts[i + 1][1] if i + 1 < len(amounts) else None)
             for i, (_v, s, e) in enumerate(amounts)]
    for i, (v, s, e) in enumerate(amounts):
        role = roles[i]
        if role == "plain" and nhl_lead:
            role = "nhl"
        if (role == "plain" and i + 1 < len(roles) and roles[i + 1] == "signing"
                and re.fullmatch(r"\s*,?\s*(?:plus|with|and)\s+an?\s*", text[e:amounts[i + 1][1]])):
            role = "base"  # "$2.4 million with a $1.5 million signing bonus"
        if role == "guaranteed":
            flags.append(f"guaranteed:{v}")
        elif role == "signing_total":
            flags.append(f"signing_bonus_total:{v}")
        elif role == "bonus_unclear":
            flags.append(f"bonus_unclear:{v}")
        elif role in ("base", "nhl"):
            vals.setdefault("base", v)
        elif role in ("minors", "minors_each", "signing", "perf"):
            vals.setdefault(role, v)
        elif role == "plain":
            vals.setdefault("total", v)
        if bare and not text[max(0, s - 2):s].strip().endswith("$") and "$" not in text[s:e]:
            flags.append("breakdown_missing_dollar_sign")
    way = "one" if ONE_WAY.search(text) else ("two" if TWO_WAY.search(text) else None)
    if way and labelled:
        vals["one_way"] = way == "one"
        if way == "one" and "total" in vals and "base" not in vals:
            vals["base"] = vals.pop("total")  # "one-way deal at $900,000": the NHL salary
    if "base" in vals and "signing" in vals and "total" not in vals:
        vals["total"] = vals["base"] + vals["signing"]
    return vals, flags


def _salary_keys(vals: dict[str, Any]) -> bool:
    return any(k in vals for k in ("base", "minors", "signing", "perf", "total"))


def _contract_seasons(claim: Claim, story: Story) -> tuple[list[str], bool]:
    """Seasons the contract covers, and whether they were derived from the signing date."""
    term = claim.term_years
    if term and claim.start_season:
        return [shift_season(claim.start_season, i) for i in range(term)], False
    if term != 1:
        return [], False
    stated = {m.group(1) for t in (clean(story.headline), clean(story.title), clean(story.summary))
              for m in ONE_YEAR_SEASON.finditer(t)}
    if len(stated) == 1:
        return [stated.pop()], False
    published = story.content_date[:10]
    # A one-year, non-entry-level deal signed between May and September is for the coming season.
    if published and "-05-" <= published[4:8] <= "-09-" and claim.contract_type != "ELC":
        return [f"{published[:4]}-{str(int(published[:4]) + 1)[2:]}"], True
    return [], False


def _last_name_in(claim: Claim, text: str) -> bool:
    last = name_key(claim.player_name).split()[-1] if claim.player_name else ""
    return bool(last) and re.search(rf"\b{re.escape(last)}\b", name_key(text)) is not None


def _may_state_terms(sent: str) -> bool:
    """A figure ('$', or '4,100,000' with the '$' missing) or "the second year as a one-way"."""
    return "$" in sent or bool(re.search(r"\d,\d{3}", sent)) or bool(ORDINAL_AS.search(sent))


def _sentences_for(story: Story, claims: list[Claim]) -> list[tuple[str, list[Claim]]]:
    """Body sentences paired with the claims they can describe.

    One-claim story: every sentence that is not about a past deal or another
    player's signing. Several claims: a sentence naming one player, and the
    sentences that follow it in the same paragraph until another player is named;
    a sentence naming several players is paired with all of them."""
    out: list[tuple[str, list[Claim]]] = []
    for para in clean(story.body).split("\n"):
        current: Claim | None = None
        for sent in split_sentences(para):
            if PAST_DEAL.search(sent) or not _may_state_terms(sent):
                named = [c for c in claims if _last_name_in(c, sent)]
                if named and len(claims) > 1:
                    current = named[0] if len(named) == 1 else None
                continue
            if len(claims) == 1:
                if SIGN_VERB.search(sent) and CONTRACT_WORD.search(sent) and any(
                        _valid_name(n := _title_name(name)) and not _last_name_in(claims[0], n)
                        for name, _deal, _paren in _segments(sent)):
                    continue
                out.append((sent, claims))
                continue
            named = [c for c in claims if _last_name_in(c, sent)]
            if named:
                current = named[0] if len(named) == 1 else None
                out.append((sent, named))
            elif current is not None:
                out.append((sent, [current]))
    return out


def _rows_from_sentence(sent: str, claim: Claim, seasons: list[str], shared: bool
                        ) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """(season, values) pairs one sentence states for ``claim``, plus claim flags."""
    rows: list[tuple[str, dict[str, Any]]] = []
    if shared:  # several players in one sentence: only "Name ($X/$Y)" belongs to one of them
        last = re.escape(claim.player_name.split()[-1])
        m = re.search(last + r"\s*\((?P<p>[^)]*\$[^)]*)\)", sent)
        if m and len(seasons) == 1:
            vals, flags = _read_segment(m.group("p"))
            if _salary_keys(vals):
                rows.append((seasons[0], vals))
            return rows, flags
        return [], []
    stated_term = term_years(sent)
    if stated_term and claim.term_years and stated_term != claim.term_years:
        return [], []
    _, flags = _read_segment(sent)
    flags = [f for f in flags if not f.startswith("breakdown_")]
    labels = list(SEASON_LABEL.finditer(sent))
    follows = AS_FOLLOWS.search(sent)
    links = [m for m in SEASON_LINK.finditer(sent)]
    if labels:
        for i, lab in enumerate(labels):
            seg = sent[lab.end():labels[i + 1].start() if i + 1 < len(labels) else len(sent)]
            seg = re.sub(r"(?:[\s&;,.]|\band\b)+$", "", seg)
            vals, seg_flags = _read_segment(seg, bare=True, labelled=True)
            flags += [f for f in seg_flags if f.startswith("breakdown_")]
            if _salary_keys(vals) or "one_way" in vals:
                rows.append((lab.group(1), vals))
    elif follows:
        vals, _ = _read_segment(follows.group(2), labelled=True)
        if _salary_keys(vals):
            rows.append((follows.group(1), vals))
    elif ORDINAL_AMOUNT.search(sent) or ORDINAL_AS.search(sent):
        for m in ORDINAL_AMOUNT.finditer(sent):
            idx = len(seasons) - 1 if m.group("ord").lower() in ("final", "last") else ORDINALS[m.group("ord").lower()]
            vals, _ = _read_segment(m.group("amt"))
            if idx < len(seasons) and vals.get("total"):
                rows.append((seasons[idx], {"total": vals["total"]}))
        for m in ORDINAL_AS.finditer(sent):
            idx = len(seasons) - 1 if m.group("ord").lower() in ("final", "last") else ORDINALS[m.group("ord").lower()]
            if idx >= len(seasons):
                continue
            vals = {"one_way": m.group("way").lower() == "one"}
            if m.group("amt"):
                vals["base"] = _amounts(m.group("amt"))[0][0]
            rows.append((seasons[idx], vals))
    elif links and any(_amounts(sent[(links[i - 1].end() if i else 0):lk.start()]) for i, lk in enumerate(links)):
        nhl_lead = bool(NHL_LEVEL_LEAD.search(sent[:_amounts(sent)[0][1]])) if _amounts(sent) else False
        for i, lk in enumerate(links):
            chunk = sent[(links[i - 1].end() if i else 0):lk.start()]
            vals, _ = _read_segment(chunk, nhl_lead=nhl_lead)
            if _salary_keys(vals):
                rows.append((lk.group(1), vals))
    elif claim.term_years == 1 and len(seasons) == 1:
        vals, _ = _read_segment(sent)
        if any(k in vals for k in ("base", "minors", "signing", "perf")):
            vals.pop("minors_each", None)
            rows.append((seasons[0], vals))
    else:
        vals, _ = _read_segment(sent)
        if "minors_each" in vals:
            rows.append(("*", {"minors": vals["minors_each"]}))
    for _season, vals in rows:
        if "minors_each" in vals:
            vals.setdefault("minors", vals.pop("minors_each"))
    return rows, flags


def _add_flag(claim: Claim, flag: str) -> None:
    if flag not in claim.flags:
        claim.flags.append(flag)


def extract_story_seasons(story: Story, claims: list[Claim], teams: dict[int, str]) -> list[SeasonClaim]:
    """Per-season rows for the claims :func:`extract_story` read from ``story``.

    Values are only what a sentence states for a season; nothing is spread over
    seasons from an AAV or a total. ``teams`` is accepted for symmetry with
    :func:`extract_story` and not used. The one side effect is on the claims'
    ``flags`` (idempotent): ``guaranteed:<usd>``, ``signing_bonus_total:<usd>``,
    ``bonus_unclear:<usd>``, ``derived:season_from_signing_date``,
    ``breakdown_missing_dollar_sign``, ``season_breakdown_mismatch`` and
    ``season_sum_mismatch``."""
    del teams
    if not claims:
        return []
    by_claim: dict[str, dict[str, tuple[dict[str, Any], list[str]]]] = {c.claim_id: {} for c in claims}
    seasons_of = {c.claim_id: _contract_seasons(c, story) for c in claims}
    placed: dict[str, set[str]] = {c.claim_id: set() for c in claims}  # seasons taken from the contract, not the text
    for sent, cs in _sentences_for(story, claims):
        for c in cs:
            seasons = seasons_of[c.claim_id][0]
            rows, flags = _rows_from_sentence(sent, c, seasons, shared=len(cs) > 1)
            for f in flags:
                _add_flag(c, f)
            acc = by_claim[c.claim_id]
            for season, vals in rows:
                targets = (list(acc) or seasons) if season == "*" else [season]
                if season == "*" or season not in sent:
                    placed[c.claim_id].update(targets)
                for s in targets:
                    have, ev = acc.setdefault(s, ({}, []))
                    for k, v in vals.items():
                        have.setdefault(k, v)
                    if sent not in ev:
                        ev.append(sent)
    out: list[SeasonClaim] = []
    for c in claims:
        acc = by_claim[c.claim_id]
        if not acc:
            continue
        seasons, derived = seasons_of[c.claim_id]
        if derived and placed[c.claim_id] & set(seasons):
            _add_flag(c, "derived:season_from_signing_date")
        if seasons and any(s not in seasons for s in acc):
            _add_flag(c, "season_breakdown_mismatch")
        stated_way = any("one_way" in vals for vals, _ in acc.values())
        derived_aav = any(f.startswith("derived:aav") for f in c.flags)
        aav = None if derived_aav else c.aav_usd
        cap = c.cap_hit_usd if c.cap_hit_usd is not None else aav
        totals = [vals.get("total") for vals, _ in acc.values()]
        expected = c.total_usd or ((c.aav_usd or c.cap_hit_usd or 0) * (c.term_years or 0)) or None
        if expected and c.term_years and len(acc) == c.term_years and all(totals):
            if abs(sum(totals) - expected) > max(0.02 * expected, 60_000):
                _add_flag(c, "season_sum_mismatch")
        for season in sorted(acc):
            vals, ev = acc[season]
            one_way = vals.get("one_way")
            if one_way is None and not stated_way:
                one_way = c.one_way
            out.append(SeasonClaim(
                claim_id=c.claim_id, contract_key=c.contract_key, season=season,
                cap_hit_usd=cap, aav_usd=aav, base_salary_usd=vals.get("base"),
                signing_bonus_usd=vals.get("signing"), performance_bonus_usd=vals.get("perf"),
                total_salary_usd=vals.get("total"), minors_salary_usd=vals.get("minors"), one_way=one_way,
                clause=None, source_id=c.source_id, confidence_tier=c.confidence_tier,
                evidence=" ".join(ev)[:400],
            ))
    return out


# --- espn rows ---------------------------------------------------------------

ESPN_POS = r"(?:Fs?|Cs?|Ds?|Gs?|LWs?|RWs?|LDs?|RDs?|Ws?)(?:/(?:F|C|D|G|LW|RW|LD|RD|W))?"
ESPN_ACTION = re.compile(
    r"(?:^|\.\s+)(?P<verb>Re-signed|Signed|Agreed to terms with)\s+(?P<names>.+?)\s+(?:to|on)\s+(?:a|an\s+|the\s+)?\s*"
    r"(?P<deal>.*?(?:contract|agreement|extension|deal|PTO)s?)\b",
    re.I,
)
ESPN_ACTION2 = re.compile(
    r"(?:^|\.\s+)(?P<verb>Signed)\s+(?P<names>[A-Z][^,.]+?)\s+(?:a|an)\s+(?P<deal>.*?(?:contract|agreement)s?)\b",
)


def _espn_names(blob: str) -> list[str]:
    blob = re.sub(rf"\b{ESPN_POS}\s+(?=[A-Z])", "", blob)
    parts = re.split(r",\s*|\s+and\s+", blob)
    return [p.strip() for p in parts if p.strip() and _valid_name(p.strip())]


def extract_espn(row: dict[str, Any]) -> tuple[list[Claim], Source | None]:
    desc = clean(row["description"])
    if not re.search(r"\b(signed|re-signed|agreed to terms)\b", desc, re.I):
        return [], None
    if EXCLUDE.search(desc):
        return [], None
    team = ESPN_ABBREV.get(row.get("team_abbrev") or "", row.get("team_abbrev"))
    published = (row.get("date") or "")[:10]
    source = Source(
        source_id=f"espn:{row['key']}",
        url=f"https://www.espn.com/nhl/transactions/_/date/{published.replace('-', '')}",
        publisher="ESPN transactions wire", author=None, published_at=row.get("date") or "",
        confidence_tier="C", headline=desc[:200], kind="espn_transactions",
    )
    claims: list[Claim] = []
    for sent in split_sentences(desc):
        m = ESPN_ACTION.search(sent) or ESPN_ACTION2.search(sent)
        if not m:
            continue
        deal = m.group("deal")
        term = term_years(deal)
        ctype = contract_type(deal, "")
        one_way = True if ONE_WAY.search(deal) else (False if TWO_WAY.search(deal) else None)
        money = classify_money(deal)
        names = _espn_names(m.group("names"))
        for name in names:
            flags = ["multi_player_story"] if len(names) > 1 else []
            total, aav = money.get("total"), money.get("aav")
            if term == 1 and total and aav is None:
                aav, flags = total, flags + ["derived:aav_one_year"]
            claims.append(Claim(
                claim_id=claim_id(source.source_id, name, None),
                contract_key=make_key(None, name, team, published),
                player_name=name, player_id=None, team=team, signed_at=published,
                contract_type=ctype, term_years=term, aav_usd=aav, cap_hit_usd=money.get("cap_hit"),
                total_usd=total, nhl_salary_usd=None, minors_salary_usd=None, one_way=one_way,
                start_season=None, end_season=None,
                re_signed=m.group("verb").lower().startswith("re-"),
                source_id=source.source_id, source_url=source.url, publisher=source.publisher,
                published_at=source.published_at, confidence_tier="C", extracted_by="regex",
                flags=flags, evidence=sent[:400],
            ))
    return claims, (source if claims else None)


# --- merge across sources ----------------------------------------------------

MERGE_FIELDS = ("contract_type", "term_years", "aav_usd", "cap_hit_usd", "total_usd",
                "nhl_salary_usd", "minors_salary_usd", "one_way", "start_season", "end_season")
WINDOW_DAYS = 45


def _group(claims: list[Claim]) -> list[list[Claim]]:
    """Group claims that describe the same contract: same player (id, else name) and
    team, published within WINDOW_DAYS of each other."""
    by_player: dict[str, list[Claim]] = {}
    id_to_name: dict[int, str] = {}
    for c in claims:
        if c.player_id:
            id_to_name.setdefault(c.player_id, name_key(c.player_name))
    for c in claims:
        who = name_key(c.player_name)
        if c.player_id:
            who = id_to_name[c.player_id]
        by_player.setdefault(who, []).append(c)
    groups: list[list[Claim]] = []
    for cs in by_player.values():
        cs.sort(key=lambda c: c.signed_at)
        open_groups: list[list[Claim]] = []
        for c in cs:
            placed = False
            for g in open_groups:
                g0 = g[0]
                same_team = c.team is None or g0.team is None or c.team == g0.team
                days = (date.fromisoformat(c.signed_at) - date.fromisoformat(g0.signed_at)).days
                same_type = c.contract_type == g0.contract_type or "PTO" not in (c.contract_type, g0.contract_type)
                if same_team and same_type and days <= WINDOW_DAYS:
                    g.append(c)
                    placed = True
                    break
            if not placed:
                open_groups.append([c])
        groups.extend(open_groups)
    return groups


def merge(claims: list[Claim]) -> list[dict[str, Any]]:
    rows = []
    for g in _group(claims):
        g.sort(key=lambda c: (c.confidence_tier, c.signed_at))
        best = g[0]
        row: dict[str, Any] = {
            "contract_key": best.contract_key,
            "player_name": best.player_name,
            "player_id": next((c.player_id for c in g if c.player_id), None),
            "team": next((c.team for c in g if c.team), None),
            "signed_at": min(c.signed_at for c in g),
        }
        conflicts = []
        for f in MERGE_FIELDS:
            vals = {getattr(c, f) for c in g if getattr(c, f) is not None}
            if f == "contract_type":
                vals = {v for v in vals if v != "standard"} or vals
            if len(vals) == 1:
                row[f] = vals.pop()
            elif len(vals) > 1:
                row[f] = None
                conflicts.append(f)
            else:
                row[f] = None
        row["re_signed"] = any(c.re_signed for c in g)
        row["conflicts"] = ";".join(conflicts)
        row["n_sources"] = len({c.source_id for c in g})
        row["best_tier"] = best.confidence_tier
        row["source_ids"] = ";".join(sorted({c.source_id for c in g}))
        row["claim_ids"] = ";".join(c.claim_id for c in g)
        row["flags"] = ";".join(sorted({f for c in g for f in c.flags}))
        for c in g:
            c.contract_key = row["contract_key"]
        rows.append(row)
    rows.sort(key=lambda r: (r["signed_at"], r["team"] or "", r["player_name"]))
    return rows


def normalize_all(stories: list[Story], espn_rows: list[dict[str, Any]], teams: dict[int, str] | None = None
                  ) -> tuple[list[Claim], list[Source], list[dict[str, Any]], list[SeasonClaim]]:
    """Claims, sources, merged contracts and per-season rows (NHL.com stories only)."""
    teams = teams or dict(STATIC_TEAMS)
    claims: list[Claim] = []
    sources: list[Source] = []
    seasons: list[SeasonClaim] = []
    for s in stories:
        cs, src = extract_story(s, teams)
        if src and cs:
            claims.extend(cs)
            sources.append(src)
            seasons.extend(extract_story_seasons(s, cs, teams))
    for r in espn_rows:
        cs, src = extract_espn(r)
        if src and cs:
            claims.extend(cs)
            sources.append(src)
    merged = merge(claims)
    keys = {c.claim_id: c.contract_key for c in claims}  # merge() re-keys claims to their contract
    for row in seasons:
        row.contract_key = keys.get(row.claim_id, row.contract_key)
    return claims, sources, merged, seasons
