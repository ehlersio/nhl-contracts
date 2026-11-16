"""The 32 active NHL team ids, from the NHL stats team list filtered by the standings, and
the player ids on their current rosters and prospect lists."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

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


# -- who played where ------------------------------------------------------------------
# Every player with an NHL game in a season, with the team(s) he played for, his
# position and birth date, from the NHL stats API (one summary and one bios request
# per season and player type). Name matching uses it to tell namesakes apart: two
# NHL players called Sebastian Aho, one of them on no current roster or prospect list.

NHL_STATS = "https://api.nhle.com/stats/rest/en/{kind}/{report}"
FIRST_REGISTRY_SEASON = 2005          # 2005-06: older than any contract in the dataset


def registry_seasons(today: date | None = None) -> list[str]:
    """Every season from 2005-06 to the current one ("2026-27" style)."""
    today = today or date.today()
    last = today.year if today.month >= 7 else today.year - 1
    return [f"{y}-{str(y + 1)[2:]}" for y in range(FIRST_REGISTRY_SEASON, last + 1)]


def _season_id(season: str) -> str:
    y = int(season[:4])
    return f"{y}{y + 1}"


def fetch_player_seasons(client: Client, seasons: list[str]) -> list[dict]:
    """Rows {player_id, season, team, full_name, position, birth_date}, one per (player,
    season, team), regular season only. A player traded mid-season gets a row for each
    team. A failed request is logged and skipped, so the result can be partial."""
    out: dict[tuple[int, str, str], dict] = {}
    for season in seasons:
        sid = _season_id(season)
        params = {"isAggregate": "false", "isGame": "false", "limit": "-1",
                  "cayenneExp": f"seasonId={sid} and gameTypeId=2"}
        for kind, name_col in (("skater", "skaterFullName"), ("goalie", "goalieFullName")):
            try:
                summary = client.get_json(NHL_STATS.format(kind=kind, report="summary"), params=params) or {}
                bios = client.get_json(NHL_STATS.format(kind=kind, report="bios"), params=params) or {}
            except RuntimeError as exc:
                log.warning("player seasons %s %s failed: %s", kind, season, exc)
                continue
            born = {int(b["playerId"]): b.get("birthDate") for b in bios.get("data", []) if b.get("playerId")}
            for r in summary.get("data", []):
                pid, name = r.get("playerId"), r.get(name_col)
                if not pid or not name:
                    continue
                position = "G" if kind == "goalie" else r.get("positionCode")
                for team in filter(None, (t.strip() for t in (r.get("teamAbbrevs") or "").split(","))):
                    out[(int(pid), season, team)] = {
                        "player_id": int(pid), "season": season, "team": team, "full_name": name,
                        "position": position, "birth_date": born.get(int(pid)),
                    }
    return [out[k] for k in sorted(out)]
