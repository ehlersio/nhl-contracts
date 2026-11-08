"""Snapshot ESPN's NHL injury list and keep each player's stints in it.

``site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries`` (free, no key)
lists every injured player by team. ``details.fantasyStatus.abbreviation`` is
``IR-LT`` for long-term injured reserve, which is what the ``ltir`` table needs;
the other statuses are kept too, since a player moving from IR-LT to IR or off
the list is how a quiet activation shows. ESPN's ``date`` is the latest news
item, not the placement date.

The list has no history, so the collector keeps one: a stint (player, team,
status) opens at the first snapshot that lists it and closes at the first one
that does not. Rows only change when a stint opens or closes (or its news date
moves), so hourly runs leave the committed JSONL mostly untouched.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from ..config import ESPN_INJURIES
from ..http import Client
from ..store import Store

log = logging.getLogger(__name__)

LONG_TERM = "IR-LT"


def stint_key(athlete_id: str, team: str | None, fantasy_status: str | None, first_seen: str) -> str:
    return hashlib.sha1(f"{athlete_id}|{team or ''}|{fantasy_status or ''}|{first_seen}".encode()).hexdigest()


def _slim(injury: dict[str, Any]) -> dict[str, Any]:
    """The injury row without links, logos, headshots and the long comment."""
    a = injury.get("athlete") or {}
    team = a.get("team") or {}
    return {
        "id": injury.get("id"),
        "status": injury.get("status"),
        "date": injury.get("date"),
        "shortComment": injury.get("shortComment"),
        "type": injury.get("type"),
        "details": injury.get("details"),
        "athlete": {
            "id": _athlete_id(a),
            "displayName": a.get("displayName"),
            "position": (a.get("position") or {}).get("abbreviation"),
            "team": {"id": team.get("id"), "abbreviation": team.get("abbreviation")},
        },
    }


def _athlete_id(a: dict[str, Any]) -> str | None:
    if a.get("id"):
        return str(a["id"])
    for link in a.get("links") or []:
        href = link.get("href") or ""
        if "/player/_/id/" in href:
            return href.split("/player/_/id/", 1)[1].split("/", 1)[0]
    return None


def parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per listed player: athlete_id, athlete_name, team_abbrev, status,
    fantasy_status, news_date, raw."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for team_block in payload.get("injuries") or []:
        for inj in team_block.get("injuries") or []:
            a = inj.get("athlete") or {}
            aid = _athlete_id(a)
            name = (a.get("displayName") or "").strip()
            if not aid or not name:
                continue
            team = (a.get("team") or {}).get("abbreviation")
            if (aid, team) in seen:  # one row per player and team; ESPN repeats none today
                continue
            seen.add((aid, team))
            fantasy = ((inj.get("details") or {}).get("fantasyStatus") or {}).get("abbreviation")
            out.append({"athlete_id": aid, "athlete_name": name, "team_abbrev": team, "status": inj.get("status"),
                        "fantasy_status": fantasy, "news_date": inj.get("date"), "raw": _slim(inj)})
    return out


def apply_snapshot(store: Store, rows: list[dict[str, Any]], taken_at: str) -> dict[str, int]:
    """Open stints for newly listed (player, team, status), close the ones no longer listed."""
    open_rows = {(r["athlete_id"], r["team_abbrev"], r["fantasy_status"]): r for r in store.open_injuries()}
    listed = set()
    opened = closed = 0
    for r in rows:
        k = (r["athlete_id"], r["team_abbrev"], r["fantasy_status"])
        listed.add(k)
        cur = open_rows.get(k)
        if cur is None:
            store.insert_injury({**r, "key": stint_key(*k, taken_at), "first_seen": taken_at, "fetched_at": taken_at})
            opened += 1
        elif cur.get("news_date") != r.get("news_date") or cur.get("status") != r.get("status"):
            store.update_injury(cur["key"], news_date=r.get("news_date"), status=r.get("status"))
    for k, cur in open_rows.items():
        if k not in listed:
            store.update_injury(cur["key"], ended_at=taken_at)
            closed += 1
    store.record_injury_snapshot(taken_at, len(rows), sum(1 for r in rows if r["fantasy_status"] == LONG_TERM))
    return {"listed": len(rows), "opened": opened, "closed": closed}


def collect(store: Store, client: Client, *, now: datetime | None = None) -> dict[str, int]:
    taken_at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    run_id = store.start_run("espn_injuries", taken_at)
    try:
        payload = client.get_json(ESPN_INJURIES)
    except RuntimeError as exc:
        log.error("espn injuries failed: %s", exc)
        store.finish_run(run_id, datetime.now(UTC).isoformat(), 1, 0, note=str(exc)[:200])
        store.commit()
        return {"listed": 0, "opened": 0, "closed": 0}
    rows = parse(payload)
    if not rows and store.open_injuries():
        # An empty list while stints are open is far more likely an ESPN hiccup than a
        # league with nobody hurt; closing every stint on it would fake activations.
        log.warning("espn injuries: empty list with open stints; snapshot skipped")
        store.finish_run(run_id, datetime.now(UTC).isoformat(), 1, 0, note="empty list skipped")
        store.commit()
        return {"listed": 0, "opened": 0, "closed": 0}
    counts = apply_snapshot(store, rows, taken_at)
    store.finish_run(run_id, datetime.now(UTC).isoformat(), 1, counts["opened"] + counts["closed"])
    store.commit()
    log.info("espn injuries: %s", counts)
    return counts
