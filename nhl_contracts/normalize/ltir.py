"""Long-term injured reserve (LTIR) stints, joined from three free sources:

- **ESPN's injury list** (``collectors.espn_injuries``, tier C): a player is on
  LTIR when ``fantasyStatus`` is ``IR-LT``. The list says who is on it now; the
  collector's stints say since when it has listed them (from 2026-11-08 on).
- **ESPN's transaction wire** (tier C): "Placed D Rasmus Sandin on long-term
  injured reserve", "Reinstated F Dakota Joshua from long-term injured reserve".
  Most activations only say "activated from injured reserve"; one of those ends
  an open LTIR stint of the same player and team (flag ``activation_from_ir``).
- **PuckPedia's public Bluesky posts** sorted as ``ltir`` by
  ``puckpedia.classify_post`` (tier D): placements ("put Sandin on LTIR"),
  activations ("activated Dewar from LTIR"), roster lines ("LTIR: Jarnkrok,
  Hakanpaa") and, often, the relief the placement gave ("They can exceed the cap
  by $3.85M", "gaining his $1.64M cap hit as available space").

One row per stint (player, team, from placement or first mention to activation).
Rules, in order of the events' dates:

- A placement opens a stint, or joins the open one of the same player and team.
  A placement in a later season than the open stint's last mention starts a new
  one; the old one ends unknown (``end_not_found``).
- An LTIR activation (ESPN "from long-term injured reserve", PuckPedia "from
  LTIR") ends the open stint. One with no open stint becomes a stint of its own
  without a start (``placement_not_found``).
- ESPN's IR-LT list dropping the player ends the stint at that snapshot.
- A mention of the player on LTIR with another team ends the open stint at that
  date (an LTIR player can be traded, and sources disagree on teams). Both rows
  carry the conflict ``team:A|B``; the later one stays active. Nothing is guessed
  beyond "the later-dated source is the newer fact".
- A stint without an end is ``active`` when ESPN lists the player as IR-LT with
  that team now, or when its last mention is in the current season. Active
  stints that ESPN's current list contradicts carry a conflict:
  ``espn_status:IR`` (ESPN lists him with another status) or ``espn_not_listed``.
  An open stint from an earlier season that ESPN doesn't list is not active
  (``end_not_found``).

``stated_relief_usd`` is the relief PuckPedia stated for the post that placed
the player, as written. A post placing several players states one figure for
all of them (``stated_relief_shared``); ``stated_source`` names the post, so a
reader summing a team's relief counts each post once. Relief is the player's cap
hit minus the team's cap space at placement, so a team under the cap gets less
than the cap hit; the dataset never computes it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import puckpedia as pp
from .contracts import ESPN_ABBREV, ESPN_POS
from .model import Source
from .text import alias_key, clean, name_key, split_sentences

LTIR_COLUMNS = [
    "player_id", "player_name", "team", "season", "since", "since_basis", "first_seen", "until", "until_basis",
    "active", "stated_relief_usd", "stated_at", "stated_source", "sources", "conflicts", "flags",
]

LONG_TERM = "IR-LT"
ESPN_INJURIES_URL = "https://www.espn.com/nhl/injuries"

# --- ESPN transaction wire ---------------------------------------------------------
IR_PHRASE = re.compile(
    r"\b(?P<prep>on|to|from|off|of)\s+(?:the\s+)?(?P<lt>long[- ]term\s+)?(?:injured\s+reserve|IR|injured\s+list)\b",
    re.I)
# "Transferred D X from injured reserve to long-term injured reserve": the first phrase is not an activation
TRANSFER_FROM = re.compile(
    r"\s+from\s+(?:the\s+)?(?:non-roster\s+list|injured\s+reserve|IR)(?=\s+to\s+(?:the\s+)?long[- ]term)", re.I)
PLACE_VERBS = {"placed", "moved", "transferred", "put"}
ACTIVATE_VERBS = {"activated", "reinstated", "returned"}
POS_PREFIX = re.compile(rf"\b{ESPN_POS}\s+(?=[A-Z])")
NAME_PIECE = re.compile(
    r"^[A-ZÀ-Þ][\w'.\-À-ÿ]*(?:\s+(?:[A-ZÀ-Þ][\w'.\-À-ÿ]*|van|de|der|von|da|di|du|la|le|St\.)){0,3}$")

# --- PuckPedia ----------------------------------------------------------------------
# lazy up to the anchor; "." is allowed for "Max Domi ($3.75M)", split_names rejects anything not a name
_NAMES = r"(?P<names>[A-ZÀ-Þ][^!?\n]{0,90}?)"
PP_PLACE = re.compile(
    r"\b(?i:put|placed|place|added|adding|moved|moving)\s+(?i:both\s+)?" + _NAMES
    + r"\s+(?i:on|to|onto)\s+(?i:season[- ]ending\s+)?LTIR\b")
PP_ACTIVATE = re.compile(
    r"\b(?i:activated|activates|removed|reinstated)\s+" + _NAMES + r"\s+(?i:from|off(?:\s+of)?)\s+LTIR\b")
PP_ACTIVATE_EXIT = re.compile(r"\b(?i:activated)\s+" + _NAMES + r"\s*(?:&|and)\s*(?i:exited)\s+LTIR\b")
PP_LIST_LINE = re.compile(r"^\s*LTIR\s*:\s*(?P<names>.+?)\s*$", re.M)
PP_WITH_ON = re.compile(r"\bwith\s+" + _NAMES + r"\s+on\s+LTIR\b")
PP_RELIEF = (
    re.compile(r"exceed\s+(?:the\s+)?cap\s+by\s+\$(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?", re.I),
    re.compile(r"\b(?:gaining|adding)\s+(?:his|their)?\s*\$(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?"
               r"\s*(?:cap\s*hit\s+)?(?:in|as)\s+(?:additional\s+|available\s+)?(?:cap\s+)?space", re.I),
    re.compile(r"\butilize\s+\$(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?\s+of\s+LTIR", re.I),
)
CONDITIONAL = re.compile(r"\b(?:if|would|could)\b", re.I)
PAREN = re.compile(r"\s*\([^)]*\)")
MONEY_TAIL = re.compile(r"\s*\$[\d.,]+\s*[KkMm]?\b")


@dataclass
class Event:
    kind: str                   # place | activate | activate_ir | listed | espn_end
    at: str                     # ISO timestamp (date-only strings sort fine against it)
    team: str | None
    name: str                   # as written: a full name, or PuckPedia's surname
    source_id: str
    source_kind: str            # espn_transactions | bluesky_puckpedia | espn_injuries
    relief: int | None = None
    relief_shared: bool = False
    flags: list[str] = field(default_factory=list)
    espn_open: bool = False     # an ESPN IR-LT stint still listed at the latest snapshot
    player_key: str = ""
    player_id: int | None = None
    full_name: str | None = None

    @property
    def day(self) -> str:
        return self.at[:10]


def season_of(day: str) -> str:
    """LTIR seasons turn on July 1 (the cap year), not in June like signings."""
    d = date.fromisoformat(day[:10])
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def _amount(num: str, unit: str | None) -> int:
    n = float(num.replace(",", ""))
    u = (unit or "").lower()
    if u == "m":
        return int(round(n * 1_000_000))
    if u == "k":
        return int(round(n * 1_000))
    if "," in num or n >= 1000:
        return int(round(n))
    return int(round(n * 1_000_000)) if n < 20 else int(round(n * 1_000))


def split_names(blob: str) -> list[str] | None:
    """Names in "Max Domi ($3.75M) & Dakota Joshua ($3.25M)", "Seguin, Lundkvist, Heiskanen",
    "D Tyler Myers and F Kiefer Sherwood". None when a piece doesn't look like a name."""
    blob = PAREN.sub("", blob)
    blob = MONEY_TAIL.sub("", blob)
    blob = POS_PREFIX.sub("", blob)
    blob = re.sub(r"^(?:both|the)\s+", "", blob.strip(), flags=re.I)
    pieces = [p.strip(" ,.;:-") for p in re.split(r"\s*(?:,|&|/|\band\b)\s*", blob)]
    pieces = [POS_PREFIX.sub("", p).strip() for p in pieces if p.strip(" ,.;:-")]
    if not pieces or not all(NAME_PIECE.match(p) for p in pieces):
        return None
    return pieces


# --- event extraction ---------------------------------------------------------------

def espn_transaction_events(rows: Iterable[dict[str, Any]]) -> tuple[list[Event], list[Source]]:
    events: list[Event] = []
    sources: list[Source] = []
    for row in rows:
        desc = clean(row.get("description") or "")
        if not re.search(r"injured|\bIR\b", desc, re.I):
            continue
        team = ESPN_ABBREV.get(row.get("team_abbrev") or "", row.get("team_abbrev"))
        sid = f"espn:{row['key']}"
        at = row.get("date") or ""
        found: list[Event] = []
        for sent in split_sentences(desc):
            s = TRANSFER_FROM.sub("", sent)
            prev_end, verb = 0, None
            for m in IR_PHRASE.finditer(s):
                chunk = s[prev_end:m.start()].strip(" ,;")
                prev_end = m.end()
                if re.match(r"\s*at\b", s[m.end():]):          # "Recalled G X from long-term IR at Y (IL)"
                    continue
                inherit = bool(re.match(r"^and\s+", chunk))
                chunk = re.sub(r"^and\s+", "", chunk)
                head, _, rest = chunk.partition(" ")
                if head.lower() in PLACE_VERBS | ACTIVATE_VERBS:
                    verb, blob = head.lower(), rest
                elif inherit and verb:
                    blob = chunk
                else:
                    verb = None
                    continue
                prep, long_term = m["prep"].lower(), bool(m["lt"])
                if verb in PLACE_VERBS and prep in ("on", "to") and long_term:
                    kind = "place"
                elif verb in ACTIVATE_VERBS and prep in ("from", "off", "of"):
                    kind = "activate" if long_term else "activate_ir"
                else:
                    continue
                blob = re.sub(r"\s+to the active roster$", "", blob)
                names = split_names(blob)
                if not names:
                    continue
                flags = []
                retro = re.search(r"retroactive to ([A-Z][a-z]+\.? \d{1,2})", s[m.end():])
                if retro and kind == "place":
                    flags.append("retroactive")
                for n in names:
                    found.append(Event(kind, at, team, n, sid, "espn_transactions", flags=list(flags)))
        if found:
            events += found
            published = at[:10]
            sources.append(Source(
                source_id=sid, url=f"https://www.espn.com/nhl/transactions/_/date/{published.replace('-', '')}",
                publisher="ESPN transactions wire", author=None, published_at=at, confidence_tier="C",
                headline=desc[:200], kind="espn_transactions"))
    return events, sources


def espn_injury_events(stints: Iterable[dict[str, Any]], latest_snapshot: str | None
                       ) -> tuple[list[Event], list[Source], dict[str, list[tuple[str | None, str | None]]]]:
    """Events for IR-LT stints, plus every player ESPN lists at the latest snapshot:
    alias_key(name) -> [(team, fantasy status)]."""
    events: list[Event] = []
    sources: list[Source] = []
    current: dict[str, list[tuple[str | None, str | None]]] = {}
    for st in stints:
        team = ESPN_ABBREV.get(st.get("team_abbrev") or "", st.get("team_abbrev"))
        name = st["athlete_name"]
        if st.get("ended_at") is None and latest_snapshot:
            current.setdefault(alias_key(name), []).append((team, st.get("fantasy_status")))
        if st.get("fantasy_status") != LONG_TERM:
            continue
        sid = f"espn_injuries:{st['key'][:16]}"
        events.append(Event("listed", st["first_seen"], team, name, sid, "espn_injuries",
                            espn_open=st.get("ended_at") is None))
        if st.get("ended_at"):
            events.append(Event("espn_end", st["ended_at"], team, name, sid, "espn_injuries"))
        raw = st.get("raw") or {}
        sources.append(Source(
            source_id=sid, url=ESPN_INJURIES_URL, publisher="ESPN injury list", author=None,
            published_at=st["first_seen"], confidence_tier="C",
            headline=(raw.get("shortComment") or f"{name}: {LONG_TERM}")[:200], kind="espn_injuries"))
    return events, sources, current


def _post_source(post: dict[str, Any], head: str) -> Source:
    return Source(
        source_id=f"bsky:{pp.rkey_of(post['uri'])}", url=pp.post_url(post["uri"]), publisher=pp.PUBLISHER,
        author="PuckPedia", published_at=post.get("created_at") or "", confidence_tier=pp.TIER,
        headline=head[:200], kind=pp.KIND)


def _unconditional(text: str, start: int) -> bool:
    """False for "If they add Heiskanen to LTIR": a conditional earlier in the sentence."""
    sent_start = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    return not CONDITIONAL.search(text[sent_start:start])


def puckpedia_events(posts: Iterable[dict[str, Any]]) -> tuple[list[Event], list[Source]]:
    events: list[Event] = []
    sources: list[Source] = []
    for post in posts:
        raw = post.get("text") or ""
        if pp.classify_post(raw) != "ltir":
            continue
        text = pp.normalize_text(raw)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        head = lines[0] if lines else ""
        teams, _ = pp.team_tags(text)
        if len(teams) != 1:          # "#NYR #MNWild & #NJDevils placed players on LTIR": no names to pin
            continue
        team = teams[0]
        sid = f"bsky:{pp.rkey_of(post['uri'])}"
        at = post.get("created_at") or ""
        found: list[Event] = []
        for kind, rx in (("place", PP_PLACE), ("activate", PP_ACTIVATE), ("activate", PP_ACTIVATE_EXIT),
                         ("listed", PP_WITH_ON)):
            for m in rx.finditer(text):
                if not _unconditional(text, m.start()):
                    continue
                names = split_names(m["names"])
                for n in names or []:
                    found.append(Event(kind, at, team, n, sid, pp.KIND, flags=[f"puckpedia_{kind}"]))
        for m in PP_LIST_LINE.finditer(text):
            for n in split_names(m["names"]) or []:
                found.append(Event("listed", at, team, n, sid, pp.KIND, flags=["puckpedia_roster_line"]))
        if not found:
            continue
        relief = None
        for rx in PP_RELIEF:
            m = rx.search(text)
            if m:
                relief = _amount(m["num"], m["unit"])
                break
        if relief is not None:
            # the relief belongs to the post's placements, or to "with X on LTIR" when it places nobody
            targets = [e for e in found if e.kind == "place"] or \
                      [e for e in found if e.kind == "listed" and "puckpedia_listed" in e.flags]
            shared = len({name_key(e.name) for e in targets}) > 1
            for e in targets:
                e.relief, e.relief_shared = relief, shared
        events += found
        sources.append(_post_source(post, head))
    return events, sources


# --- who is who -----------------------------------------------------------------------

class Resolver:
    """Full names and NHL ids for the names the sources use. PuckPedia mostly writes
    surnames, matched against the full names tied to the same team in the same season
    (contracts, the current roster list, ESPN's rows); one candidate or nothing."""

    def __init__(self, players: dict[int, tuple[str, str | None]], contracts: list[dict[str, Any]],
                 full_name_events: list[Event], current_season: str) -> None:
        self.ids_by_alias: dict[str, set[int]] = {}
        self.team_of_id: dict[int, str | None] = {}
        # (team, season) -> {alias_key: full name}
        self.names: dict[tuple[str | None, str], dict[str, str]] = {}
        for pid, (name, team) in players.items():
            self.ids_by_alias.setdefault(alias_key(name), set()).add(pid)
            self.team_of_id[pid] = team
            self._add(team, current_season, name)
        for c in contracts:
            name = c.get("player_name") or ""
            if not name:
                continue
            if c.get("player_id"):
                self.ids_by_alias.setdefault(alias_key(name), set()).add(int(c["player_id"]))
            start, end = c.get("start_season"), c.get("end_season") or c.get("start_season")
            if start:
                y0, y1 = int(start[:4]), int((end or start)[:4])
                for y in range(y0, min(y1, y0 + 15) + 1):
                    self._add(c.get("team"), f"{y}-{str(y + 1)[2:]}", name)
        for e in full_name_events:
            self._add(e.team, season_of(e.day), e.name)

    def _add(self, team: str | None, season: str, name: str) -> None:
        self.names.setdefault((team, season), {})[alias_key(name)] = name

    def full_name(self, name: str, team: str | None, day: str) -> str | None:
        if len(name.split()) >= 2:
            return name
        want = name_key(name.replace("-", " "))
        if not want:
            return None
        season = season_of(day)
        y = int(season[:4])
        near = [f"{x}-{str(x + 1)[2:]}" for x in (y - 1, y + 1)]
        # the team that season, then the team a season either side, then the whole league that season
        for pools in ([(team, season)], [(team, s) for s in near],
                      [k for k in self.names if k[1] == season]):
            hits: dict[str, str] = {}
            for pool in pools:
                for k, v in self.names.get(pool, {}).items():
                    if (" " + name_key(v.replace("-", " "))).endswith(" " + want):
                        hits[k] = v
            if len(hits) == 1:
                return next(iter(hits.values()))
            if len(hits) > 1:
                return None          # two players with that surname: don't pick one
        return None

    def player_id(self, full: str, team: str | None) -> int | None:
        ids = self.ids_by_alias.get(alias_key(full), set())
        if len(ids) > 1 and team:
            ids = {i for i in ids if self.team_of_id.get(i) == team} or ids
        return next(iter(ids)) if len(ids) == 1 else None


# --- stints -----------------------------------------------------------------------------

@dataclass
class Stint:
    key: str
    team: str | None
    events: list[Event] = field(default_factory=list)
    until: str | None = None
    until_basis: str | None = None
    flags: set[str] = field(default_factory=set)
    conflicts: set[str] = field(default_factory=set)

    @property
    def last_mention(self) -> str:
        return max(e.at for e in self.events if e.kind != "espn_end")


def _close(st: Stint, at: str, basis: str, ev: Event | None = None) -> None:
    st.until, st.until_basis = at[:10], basis
    if ev is not None:
        st.events.append(ev)


def build_stints(events: list[Event]) -> list[Stint]:
    by_player: dict[str, list[Event]] = {}
    for e in events:
        by_player.setdefault(e.player_key, []).append(e)
    order = {"place": 0, "listed": 1, "activate_ir": 2, "activate": 3, "espn_end": 4}
    stints: list[Stint] = []
    for pkey, evs in by_player.items():
        evs.sort(key=lambda e: (e.at, order[e.kind]))
        open_: dict[str | None, Stint] = {}
        for e in evs:
            if e.kind in ("place", "listed"):
                for team, other in list(open_.items()):
                    if team != e.team and season_of(other.last_mention) < season_of(e.day):
                        other.flags.add("end_not_found")   # an old stint nothing closed: not a conflict
                        open_.pop(team)
                    elif team != e.team:
                        _close(other, e.at, "later_team")
                        conflict = f"team:{'|'.join(sorted(str(t) for t in (team, e.team)))}"
                        other.conflicts.add(conflict)
                        open_.pop(team)
                        nxt = open_.get(e.team)
                        if nxt is None:
                            nxt = Stint(pkey, e.team)
                            open_[e.team] = nxt
                            stints.append(nxt)
                        nxt.conflicts.add(conflict)
                st = open_.get(e.team)
                if st is not None and e.kind == "place" and season_of(e.day) > season_of(st.last_mention):
                    st.flags.add("end_not_found")
                    open_.pop(e.team)
                    st = None
                if st is None:
                    st = Stint(pkey, e.team)
                    open_[e.team] = st
                    stints.append(st)
                st.events.append(e)
            elif e.kind == "activate":
                st = open_.pop(e.team, None)
                if st is None:
                    st = Stint(pkey, e.team, flags={"placement_not_found"})
                    stints.append(st)
                _close(st, e.at, e.source_kind, e)
            elif e.kind == "activate_ir":
                st = open_.get(e.team)
                if st is not None:
                    open_.pop(e.team)
                    st.flags.add("activation_from_ir")
                    _close(st, e.at, e.source_kind, e)
            elif e.kind == "espn_end":
                st = open_.get(e.team)
                if st is not None and any(x.source_kind == "espn_injuries" for x in st.events) \
                        and st.last_mention <= e.at:
                    open_.pop(e.team)
                    _close(st, e.at, "espn_injuries", e)
    return [s for s in stints if any(e.kind != "espn_end" for e in s.events)]


def ltir_rows(stints: list[Stint], current: dict[str, list[tuple[str | None, str | None]]],
              latest_snapshot: str | None, as_of: str) -> list[dict[str, Any]]:
    now_season = season_of(as_of)
    rows = []
    for st in stints:
        evs = [e for e in st.events if e.kind != "espn_end"]
        first = min(evs, key=lambda e: e.at)
        places = [e for e in evs if e.kind == "place"]
        espn_place = [e for e in places if e.source_kind == "espn_transactions"]
        since_ev = min(espn_place or places, key=lambda e: e.at) if places else None
        flags = set(st.flags)
        for e in evs:
            flags.update(f for f in e.flags if f == "retroactive")
        on_espn = any(e.espn_open for e in evs)
        active = False
        conflicts = set(st.conflicts)
        if st.until is None:
            if on_espn:
                active = True
            elif season_of(st.last_mention) == now_season:
                active = True
                if latest_snapshot and latest_snapshot > st.last_mention:
                    listed = current.get(alias_key(first.full_name or first.name), [])
                    other = sorted({s or "?" for t, s in listed if t == st.team and s != LONG_TERM})
                    if other:
                        conflicts.add(f"espn_status:{'|'.join(other)}")
                    elif not any(t == st.team for t, _ in listed):
                        conflicts.add("espn_not_listed")
            else:
                flags.add("end_not_found")
        stated = [e for e in evs if e.relief is not None]
        last_stated = max(stated, key=lambda e: e.at) if stated else None
        if last_stated is not None and last_stated.relief_shared:
            flags.add("stated_relief_shared")
        if not first.full_name:
            flags.add("name_unresolved")
        start_day = since_ev.day if since_ev else first.day
        if date.fromisoformat(start_day).month in (7, 8):
            flags.add("offseason")
        rows.append({
            "player_id": first.player_id,
            "player_name": first.full_name or first.name,
            "team": st.team,
            "season": season_of(start_day),
            "since": since_ev.day if since_ev else None,
            "since_basis": since_ev.source_kind if since_ev else None,
            "first_seen": first.day,
            "until": st.until,
            "until_basis": st.until_basis,
            "active": active,
            "stated_relief_usd": last_stated.relief if last_stated else None,
            "stated_at": last_stated.day if last_stated else None,
            "stated_source": last_stated.source_id if last_stated else None,
            "sources": ";".join(sorted({e.source_id for e in st.events})),
            "conflicts": ";".join(sorted(conflicts)),
            "flags": ";".join(sorted(flags)),
        })
    rows.sort(key=lambda r: (r["team"] or "", r["since"] or r["first_seen"], r["player_name"]))
    return rows


def normalize(espn_rows: Iterable[dict[str, Any]], posts: Iterable[dict[str, Any]],
              injury_stints: Iterable[dict[str, Any]], latest_snapshot: str | None,
              players: dict[int, tuple[str, str | None]], contracts: list[dict[str, Any]],
              as_of: str | None = None) -> tuple[list[dict[str, Any]], list[Source]]:
    """Rows of LTIR_COLUMNS and their sources."""
    as_of = as_of or latest_snapshot or datetime.now().date().isoformat()
    t_events, t_sources = espn_transaction_events(espn_rows)
    i_events, i_sources, current = espn_injury_events(injury_stints, latest_snapshot)
    p_events, p_sources = puckpedia_events(posts)
    full_events = t_events + i_events
    resolver = Resolver(players, contracts, full_events, season_of(as_of))
    events = full_events + p_events
    for e in events:
        e.full_name = resolver.full_name(e.name, e.team, e.day)
        e.player_id = resolver.player_id(e.full_name, e.team) if e.full_name else None
        if e.player_id:
            e.player_key = f"id:{e.player_id}"
        elif e.full_name:
            e.player_key = f"name:{alias_key(e.full_name)}"
        else:
            e.player_key = f"surname:{e.team}:{name_key(e.name)}"
    # a stint needs an LTIR fact: generic IR activations alone only close stints
    keyed = {e.player_key for e in events if e.kind != "activate_ir"}
    events = [e for e in events if e.player_key in keyed]
    rows = ltir_rows(build_stints(events), current, latest_snapshot, as_of)
    return rows, t_sources + i_sources + p_sources
