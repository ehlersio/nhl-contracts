"""The 32 active NHL team ids, from the NHL stats team list filtered by the standings."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .config import NHL_STANDINGS, NHL_TEAMS, STATIC_TEAMS
from .http import Client
from .store import Store

log = logging.getLogger(__name__)


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
