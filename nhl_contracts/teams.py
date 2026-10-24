"""The 32 active NHL team ids, from the NHL stats team list filtered by the standings, and
the player ids on their current rosters and prospect lists."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .config import NHL_STANDINGS, NHL_TEAMS, STATIC_TEAMS
from .http import Client
from .store import Store

log = logging.getLogger(__name__)

NHL_ROSTER = "https://api-web.nhle.com/v1/roster/{abbr}/current"
NHL_PROSPECTS = "https://api-web.nhle.com/v1/prospects/{abbr}"
ROSTER_GROUPS = ("forwards", "defensemen", "goalies")


def fetch_teams(client: Client) -> dict[int, tuple[str, str]]:
    """Return {team_id: (abbrev, full_name)} for every team in the current standings."""
    standings = client.get_json(NHL_STANDINGS)
    active = {s["teamAbbrev"]["default"] for s in standings.get("standings", [])}
    if not active:
        raise RuntimeError("standings/now returned no teams")
    teams = client.get_json(NHL_TEAMS)
    out: dict[int, tuple[str, str]] = {}
    for t in teams.get("data", []):
        if t.get("triCode") in active:
            out[int(t["id"])] = (t["triCode"], t.get("fullName", ""))
    missing = active - {ab for ab, _ in out.values()}
    if missing:
        log.warning("standings teams without a stats-team row: %s", sorted(missing))
    return out


def load_teams(store: Store, client: Client | None) -> dict[int, str]:
    """Live when a client is given (and cached in the store), otherwise cached or static."""
    if client is not None:
        try:
            teams = fetch_teams(client)
            store.set_teams(teams, datetime.now(UTC).isoformat())
            store.commit()
            return {tid: ab for tid, (ab, _) in teams.items()}
        except Exception as exc:  # noqa: BLE001 - fall back, the ids are stable
            log.warning("live team list failed (%s); using cached/static ids", exc)
    cached = store.teams()
    return cached or dict(STATIC_TEAMS)


def _people(payload: dict) -> list[tuple[int, str]]:
    """(id, "First Last") for every player in a roster or prospects payload."""
    out = []
    for group in ROSTER_GROUPS:
        for p in payload.get(group) or []:
            pid = p.get("id")
            name = " ".join(x for x in ((p.get("firstName") or {}).get("default", ""),
                                        (p.get("lastName") or {}).get("default", "")) if x)
            if pid and name:
                out.append((int(pid), name))
    return out


def fetch_player_ids(client: Client, teams: dict[int, str]) -> dict[int, tuple[str, str, str]]:
    """Return {player_id: (full name, team abbrev, kind)} from every club's current roster
    ("roster") and prospect list ("prospect"). Each abbrev is fetched once (Utah has two
    team ids). A player on one club's roster and another's prospect list keeps the roster
    club. A failed request is logged and skipped, so the result can be partial."""
    out: dict[int, tuple[str, str, str]] = {}
    done: set[str] = set()
    for tid in sorted(teams):
        abbr = teams[tid]
        if not abbr or abbr in done:
            continue
        done.add(abbr)
        for kind, url in (("roster", NHL_ROSTER.format(abbr=abbr)), ("prospect", NHL_PROSPECTS.format(abbr=abbr))):
            try:
                payload = client.get_json(url)
            except RuntimeError as exc:
                log.warning("player list %s failed: %s", url, exc)
                continue
            for pid, name in _people(payload or {}):
                out.setdefault(pid, (name, abbr, kind))
    return out
