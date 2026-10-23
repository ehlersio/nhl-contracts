"""Collect ESPN's NHL transaction wire for a date range and store the raw rows.

ESPN gives term and contract type (entry-level, two-way, extension, PTO) for
nearly every signing but almost never a dollar figure, so it is a cross-check
and a safety net for clubs whose NHL.com tagging misses a signing.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..config import ESPN_TRANSACTIONS
from ..http import Client
from ..store import Store

log = logging.getLogger(__name__)


def row_key(date_str: str, team: str, description: str) -> str:
    return hashlib.sha1(f"{date_str}|{team}|{description}".encode()).hexdigest()


def strip_row(t: dict[str, Any]) -> dict[str, Any]:
    team = dict(t.get("team") or {})
    team.pop("logos", None)
    return {**t, "team": team}


def normalize_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for t in payload.get("transactions", []):
        team = t.get("team") or {}
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        rows.append({
            "key": row_key(t.get("date", ""), team.get("abbreviation", ""), desc),
            "date": t.get("date", ""),
            "team_abbrev": team.get("abbreviation"),
            "team_id": team.get("id"),
            "description": desc,
            "raw": strip_row(t),
        })
    return rows


def date_chunks(start: date, end: date, days: int = 31):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def collect(store: Store, client: Client, start: date, end: date) -> dict[str, int]:
    started = datetime.now(UTC).isoformat()
    run_id = store.start_run("espn_transactions", started)
    requests = 0
    new = 0
    for a, b in date_chunks(start, end):
        params = {"dates": f"{a:%Y%m%d}-{b:%Y%m%d}", "limit": 1000}
        try:
            payload = client.get_json(ESPN_TRANSACTIONS, params)
        except RuntimeError as exc:
            log.error("espn %s failed: %s", params["dates"], exc)
            continue
        requests += 1
        rows = normalize_rows(payload)
        if payload.get("pageCount", 1) > 1:
            log.warning("espn %s has %s pages; only the first was read", params["dates"],
                        payload.get("pageCount"))
        new += store.upsert_espn(rows, datetime.now(UTC).isoformat())
        store.commit()
    store.finish_run(run_id, datetime.now(UTC).isoformat(), requests, new)
    store.commit()
    log.info("espn: %d requests, %d new rows", requests, new)
    return {"requests": requests, "new": new}
