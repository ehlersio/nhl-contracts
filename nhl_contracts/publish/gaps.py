"""gaps.csv: current NHL roster players the dataset does not cover, with what the raw
store knows about them, so the remaining work is a list rather than a percentage.

One row per roster player (not prospects) whose current contract is missing or
incomplete. ``gap`` says what is missing:

- ``no_contract``   no contract row covers the current season
- ``no_money``      a contract row exists but neither cap hit nor AAV is known
- ``no_structure``  money is known but no per-season rows exist

``status`` says where to look: ``older_rows_only`` (the player is in the dataset,
but no row spans this season), ``in_raw_store`` (an NHL.com story, ESPN row or
Bluesky post mentions the player since last July: a parsing or matching gap) or
``absent`` (no source mentions them).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from ..normalize.text import season_from_date, strip_accents
from ..store import Store

GAP_COLUMNS = ["player_id", "player_name", "team", "gap", "status", "last_contract_end", "espn_hint", "forge_hint",
               "bsky_hint", "checked_season"]


def _hints(store: Store, name: str, since: str) -> tuple[str, str, str]:
    surname = strip_accents(name.split()[-1])
    like = f"%{surname}%"
    espn = store.conn.execute(
        """SELECT date, description FROM espn_transactions WHERE date >= ? AND description LIKE ?
           AND (description LIKE '%igned%' OR description LIKE '%greed to terms%') ORDER BY date DESC LIMIT 1""",
        (since, like)).fetchone()
    forge = store.conn.execute(
        """SELECT content_date, headline FROM forge_stories WHERE content_date >= ? AND (headline LIKE ? OR body LIKE ?)
           AND (lower(headline) LIKE '%sign%' OR lower(headline) LIKE '%agree%' OR lower(headline) LIKE '%contract%')
           ORDER BY content_date DESC LIMIT 1""", (since, like, like)).fetchone()
    bsky = store.conn.execute(
        """SELECT created_at, text FROM bsky_posts WHERE created_at >= ? AND text LIKE ?
           AND (text LIKE '%signed%' OR text LIKE '%ELC%') ORDER BY created_at DESC LIMIT 1""",
        (since, like)).fetchone()

    def fmt(r: Any) -> str:
        return f"{r[0][:10]} {r[1][:120]}".replace("\n", " ") if r else ""

    return fmt(espn), fmt(forge), fmt(bsky)


def find_gaps(store: Store, contracts: Iterable[dict[str, Any]], seasons: Iterable[dict[str, Any]],
              today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    season = season_from_date(today)
    since = f"{int(season[:4]) - 1}-07-01"
    roster = {pid: (name, team) for pid, (name, team, kind) in store.players_with_kind().items() if kind == "roster"}
    rows = list(contracts)
    skeys = {s["contract_key"] for s in seasons}
    by_pid: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("player_id"):
            by_pid.setdefault(r["player_id"], []).append(r)
    out = []
    for pid, (name, team) in sorted(roster.items(), key=lambda kv: (kv[1][1], kv[1][0])):
        mine = by_pid.get(pid, [])
        current = [r for r in mine if r.get("end_season") and r["end_season"] >= season
                   and (r.get("start_season") or "0000") <= season]
        if current:
            if not any(r.get("aav_usd") or r.get("cap_hit_usd") for r in current):
                gap = "no_money"
            elif not any(r["contract_key"] in skeys for r in current):
                gap = "no_structure"
            else:
                continue
            status = "has_contract"
        else:
            gap = "no_contract"
            espn, forge, bsky = _hints(store, name, since)
            status = "older_rows_only" if mine else ("in_raw_store" if (espn or forge or bsky) else "absent")
        if gap != "no_contract":
            espn, forge, bsky = _hints(store, name, since)
        ends = [r["end_season"] for r in mine if r.get("end_season")]
        out.append({
            "player_id": pid, "player_name": name, "team": team, "gap": gap, "status": status,
            "last_contract_end": max(ends) if ends else "", "espn_hint": espn, "forge_hint": forge,
            "bsky_hint": bsky, "checked_season": season,
        })
    return out


def write_gaps(gaps: list[dict[str, Any]], dist: Path) -> dict[str, int]:
    dist.mkdir(parents=True, exist_ok=True)
    with (dist / "gaps.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GAP_COLUMNS)
        w.writeheader()
        for g in gaps:
            w.writerow({c: g.get(c, "") for c in GAP_COLUMNS})
    (dist / "gaps.json").write_text(json.dumps(gaps, ensure_ascii=False, indent=0), encoding="utf-8")
    counts = {"n_gaps": len(gaps)}
    for kind in ("no_contract", "no_money", "no_structure"):
        counts[f"n_gaps_{kind}"] = sum(1 for g in gaps if g["gap"] == kind)
    return counts
