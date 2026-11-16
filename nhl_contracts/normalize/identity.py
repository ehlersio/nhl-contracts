"""Who is who: the NHL player id behind a name a source wrote.

Every name-to-id lookup in the pipeline goes through ``PlayerIndex``. It knows two
kinds of player:

- the current roster and prospect lists (``players`` in the store): id, name, club;
- every player with an NHL regular-season game since 2005-06 (``player_seasons``):
  id, name, the club(s) of each season, position and birth date.

A name that belongs to one known player resolves to him, as before, unless a hard
fact rules him out (a birth date more than a year off, goalie versus skater, an age
at which no one signs); his club and his years away are not checked (flag
``player_id_ruled_out`` when a fact does rule him out).

A name shared by several players (namesakes: two NHL players called
Sebastian Aho, a centre born 1997 who has only played for Carolina and a
defenceman born 1996 who played for the Islanders and was a Penguin in 2024-26) is
resolved only when exactly one of them fits every fact the source states, and fits
at least one positively:

- birth date (archived CapFriendly pages state it; within a year is "unknown"),
- position group, forward / defence / goalie (ESPN's wire writes "D Sebastian Aho"),
- club: the club the source names appears in his seasons around the signing, and he
  did not play that season and the next for other clubs only (a current roster or
  prospect listing counts as a season with that club),
- age: 17 to 46 at signing, and not four seasons past his last NHL game while on no
  current list.

Otherwise the id stays empty and the claim is flagged ``player_name_ambiguous``
(``player_id_ruled_out`` when every namesake is ruled out): a namesake is named,
never picked. ``merge.assign_player_ids`` gives an ambiguous claim the id of another
source's claim on the same contract (same club, same signing window) when that id is
one of its namesakes (``player_id_from_same_contract``).

Flags on a claim whose id came from its name: ``player_id_from_roster`` (a current
roster or prospect list), ``player_id_from_nhl_seasons`` (a player with NHL games
since 2005-06 who is on no current list), ``player_id_by_alias`` (the name matched
through the alias key only), ``player_id_by_namesake_keys``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .text import alias_key, name_key, season_from_date, shift_season

# The franchise behind a historical code, for matching seasons only.
FRANCHISE = {"PHX": "UTA", "ARI": "UTA", "ATL": "WPG"}
POSITION_GROUP = {"C": "F", "L": "F", "R": "F", "LW": "F", "RW": "F", "W": "F", "F": "F",
                  "D": "D", "LD": "D", "RD": "D", "G": "G"}
MIN_AGE, MAX_AGE = 17, 46
# Sources misprint birth dates now and then: CapFriendly has Kiefer Sherwood born
# 21 March 1995 (NHL: 31 March), William Eklund 2002-12-10 (NHL: 2002-10-12, day and
# month swapped), Mason Marchment 1995-03-06 (NHL: 1995-06-18). A date within a year
# is neither a fit nor a miss; the two Sebastian Ahos are 17 months apart.
BIRTH_DATE_SLACK_DAYS = 366
MAX_SEASONS_AWAY = 4

FLAG_ROSTER = "player_id_from_roster"
FLAG_HISTORY = "player_id_from_nhl_seasons"
FLAG_ALIAS = "player_id_by_alias"
FLAG_SAME_CONTRACT = "player_id_from_same_contract"
FLAG_NAMESAKE = "player_id_by_namesake_keys"
FLAG_AMBIGUOUS = "player_name_ambiguous"
FLAG_RULED_OUT = "player_id_ruled_out"


def franchise(team: str | None) -> str | None:
    return FRANCHISE.get(team, team) if team else None


def position_group(position: str | None) -> str | None:
    """"D", "Ds", "LW/C", "RW", "G" -> F | D | G; None when unknown."""
    if not position:
        return None
    first = position.split("/")[0].strip().upper()
    if first not in POSITION_GROUP and first.endswith("S"):
        first = first[:-1]
    return POSITION_GROUP.get(first)


@dataclass
class Match:
    player_id: int | None
    flag: str | None
    candidates: tuple[int, ...] = ()


@dataclass
class _Player:
    name: str
    teams: dict[str, set[str]] = field(default_factory=dict)   # season -> franchises (games or current list)
    active: set[str] = field(default_factory=set)              # seasons with NHL games (or listed now)
    birth_date: str | None = None
    position: str | None = None                               # F | D | G
    listed: bool = False                                       # on a current roster or prospect list


class PlayerIndex:
    def __init__(self, players: dict[int, tuple] | None = None, seasons: Iterable[dict[str, Any]] = (),
                 current_season: str | None = None) -> None:
        self.current_season = current_season or season_from_date(date.today())
        self.people: dict[int, _Player] = {}
        self.by_name: dict[str, set[int]] = {}
        self.by_alias: dict[str, set[int]] = {}
        for pid, v in (players or {}).items():
            p = self._add(int(pid), v[0])
            p.listed = True
            if v[1]:
                p.teams.setdefault(self.current_season, set()).add(franchise(v[1]))
                p.active.add(self.current_season)
        for r in seasons:
            p = self._add(int(r["player_id"]), r["full_name"])
            p.teams.setdefault(r["season"], set()).add(franchise(r["team"]))
            p.active.add(r["season"])
            p.birth_date = p.birth_date or r.get("birth_date")
            p.position = p.position or position_group(r.get("position"))

    @classmethod
    def from_store(cls, store: Any) -> PlayerIndex:
        return cls(store.players(), store.player_seasons())

    def _add(self, pid: int, name: str) -> _Player:
        p = self.people.get(pid)
        if p is None:
            p = self.people[pid] = _Player(name)
        self.by_name.setdefault(name_key(name), set()).add(pid)
        self.by_alias.setdefault(alias_key(name), set()).add(pid)
        return p

    def add_name(self, pid: int, name: str) -> None:
        """A player known only by an id a source tagged (no seasons, no club)."""
        self._add(int(pid), name)

    def add_contract(self, pid: int, name: str, team: str | None, start: str | None, end: str | None) -> None:
        """A contract with an id: its club counts for the seasons it covers (not as games)."""
        p = self._add(int(pid), name)
        if team and start:
            end = end if end and end >= start else start
            for i in range(min(int(end[:4]) - int(start[:4]), 15) + 1):
                p.teams.setdefault(shift_season(start, i), set()).add(franchise(team))

    def namesakes(self) -> dict[str, list[int]]:
        """Normalized names that belong to more than one player id."""
        return {k: sorted(v) for k, v in self.by_name.items() if len(v) > 1}

    def name(self, pid: int) -> str | None:
        p = self.people.get(pid)
        return p.name if p else None

    # -- facts ------------------------------------------------------------------------

    def _checks(self, pid: int, team: str | None, day: str | None, position: str | None,
                birth_date: str | None, seasons: tuple[str, ...], namesakes: bool = True) -> list[bool | None]:
        """True (fits), False (ruled out) or None (unknown) for each fact the source states.
        Without namesakes the club and the years since his last NHL game are not checked
        (journeymen come back on tryouts and two-way deals), and only goalie versus skater
        counts for position (sources mislabel forwards and defencemen now and then)."""
        p = self.people[pid]
        out: list[bool | None] = []
        if birth_date and p.birth_date:
            gap = abs((date.fromisoformat(birth_date) - date.fromisoformat(p.birth_date)).days)
            out.append(True if gap == 0 else None if gap <= BIRTH_DATE_SLACK_DAYS else False)
        group = position_group(position)
        if group and p.position and (namesakes or "G" in (group, p.position)):
            out.append(group == p.position)
        if day and p.birth_date:
            age = (date.fromisoformat(day[:10]) - date.fromisoformat(p.birth_date)).days / 365.25
            if not MIN_AGE <= age <= MAX_AGE:
                out.append(False)
        core, window = set(seasons), set(seasons)
        season = season_from_date(date.fromisoformat(day[:10])) if day else min(seasons, default=None)
        if (namesakes and season and p.active and not p.listed
                and max(p.active) < shift_season(season, -MAX_SEASONS_AWAY)):
            out.append(False)                 # long gone from the league
        if day:
            core |= {season, shift_season(season, 1)}
            window |= core | {shift_season(season, -1), shift_season(season, 2)}
        team = franchise(team) if namesakes else None
        if team and core:
            if any(team in p.teams.get(s, ()) for s in window):
                out.append(True)
            elif core & p.active:
                out.append(False)             # with other clubs only around the signing
        return out

    # -- lookups ----------------------------------------------------------------------

    def candidates(self, name: str) -> set[int]:
        """Every id with this name, exact or by alias key: "Nathan Smith" is the NHL's
        "Nathan Smith" (born 1982) and its "Nate Smith" (born 1998)."""
        return self.by_name.get(name_key(name), set()) | self.by_alias.get(alias_key(name), set())

    def _flag(self, pid: int, name: str) -> str:
        if pid not in self.by_name.get(name_key(name), ()):
            return FLAG_ALIAS
        return FLAG_ROSTER if self.people[pid].listed else FLAG_HISTORY

    def resolve(self, name: str, team: str | None = None, day: str | None = None, *,
                position: str | None = None, birth_date: str | None = None,
                start_season: str | None = None, end_season: str | None = None) -> Match:
        ids = self.candidates(name)
        if not ids:
            return Match(None, None)
        seasons: tuple[str, ...] = ()
        if start_season and end_season and start_season <= end_season:
            n = int(end_season[:4]) - int(start_season[:4])
            seasons = tuple(shift_season(start_season, i) for i in range(min(n, 15) + 1))
        # The club only tells namesakes apart: sources disagree about one player's club
        # now and then, and that is a conflict to keep, not a reason to drop his id.
        checks = {pid: self._checks(pid, team, day, position, birth_date, seasons, len(ids) > 1) for pid in ids}
        viable = [pid for pid, cs in checks.items() if False not in cs]
        if not viable:
            return Match(None, FLAG_RULED_OUT, tuple(sorted(ids)))
        if len(ids) == 1:
            pid = viable[0]
            return Match(pid, self._flag(pid, name), (pid,))
        fits = [pid for pid in viable if True in checks[pid]]
        if len(viable) == 1 and fits:
            return Match(fits[0], FLAG_NAMESAKE, tuple(sorted(ids)))
        return Match(None, FLAG_AMBIGUOUS, tuple(sorted(ids)))
