"""Per contract-season: is there a trade clause, none, or don't we know?

``clauses`` only lists clauses a source stated, so a season without a clause row
can mean "none" or "not reported". This module tells the two apart where a
source allows it, one row per season of every contract:

  status       basis                     when
  clause       source                    a clause row covers the season
  none         stated                    a PuckPedia post says the deal has no trade protection
                                         (the post's claim carries ``no_trade_protection``)
  none         capfriendly_2024          an archived CapFriendly page lists the season with a
                                         blank Clause cell (it listed every season's clause)
  none_likely  breakdown_without_clause  a PuckPedia year-by-year breakdown covers the season and
                                         names no clause for it (PuckPedia lists clauses when a
                                         deal has one, but this is an inference, not a statement)
  unknown      (empty)                   nothing above

The first match wins, top to bottom. A clause row never loses to a "none": a
source that states a clause beats one that leaves it out.

Consumers can settle more seasons from the CBA: a no-trade or no-move clause
only takes effect in a player's UFA years (27 by June 30, or 7 accrued
seasons), and accrued seasons start at 18, so a player under 25 on the June 30
before a season can't have one in effect. The dataset has no birth dates, so
it leaves that rule to the reader (the EyeWall pipeline applies it).
"""

from __future__ import annotations

from typing import Any

from .model import Claim, SeasonClaim
from .text import shift_season

STATUS_COLUMNS = ["contract_key", "season", "status", "basis", "source_ids"]

WAYBACK_PREFIX = "wayback:"
BLUESKY_PREFIX = "bsky:"


def _seasons(start: str | None, end: str | None) -> list[str]:
    if not start or not end or end < start:
        return []
    out, s = [], start
    while s <= end and len(out) < 20:
        out.append(s)
        s = shift_season(s, 1)
    return out


def _covers(row: dict[str, Any], season: str, start: str, end: str) -> bool:
    return (row.get("season_from") or start) <= season <= (row.get("season_to") or end)


def clause_status(contracts: list[dict[str, Any]], clauses: list[dict[str, Any]],
                  season_claims: list[SeasonClaim], claims: list[Claim]) -> list[dict[str, Any]]:
    """Rows of STATUS_COLUMNS. ``season_claims`` must already carry merged contract
    keys (``merge_seasons`` rewrites them in place); ``claims`` are the claims the
    contracts were merged from (their ``claim_ids``)."""
    no_protection = {c.claim_id: c.source_id for c in claims if "no_trade_protection" in c.flags}
    clauses_by_key: dict[str, list[dict[str, Any]]] = {}
    for c in clauses:
        clauses_by_key.setdefault(c["contract_key"], []).append(c)

    # (contract, season) -> archived CapFriendly season rows / PuckPedia breakdown rows
    capfriendly: dict[tuple[str, str], list[SeasonClaim]] = {}
    breakdown: dict[tuple[str, str], set[str]] = {}
    for s in season_claims:
        k = (s.contract_key, s.season)
        if s.source_id.startswith(WAYBACK_PREFIX):
            capfriendly.setdefault(k, []).append(s)
        elif s.source_id.startswith(BLUESKY_PREFIX):
            breakdown.setdefault(k, set()).add(s.source_id)

    rows = []
    for c in contracts:
        key, start, end = c["contract_key"], c.get("start_season"), c.get("end_season")
        stated = sorted({no_protection[cid] for cid in (c.get("claim_ids") or "").split(";") if cid in no_protection})
        for season in _seasons(start, end):
            covering = [r for r in clauses_by_key.get(key, []) if _covers(r, season, start, end)]
            cf = capfriendly.get((key, season), [])
            if covering:
                status, basis = "clause", "source"
                sources = sorted({sid for r in covering for sid in (r.get("source_ids") or "").split(";") if sid})
            elif stated:
                status, basis, sources = "none", "stated", stated
            elif cf and not any((s.clause or "").strip() for s in cf):
                status, basis = "none", "capfriendly_2024"
                sources = sorted({s.source_id for s in cf})
            elif (key, season) in breakdown:
                status, basis, sources = "none_likely", "breakdown_without_clause", sorted(breakdown[(key, season)])
            else:
                status, basis, sources = "unknown", None, []
            rows.append({"contract_key": key, "season": season, "status": status, "basis": basis,
                         "source_ids": ";".join(sources)})
    rows.sort(key=lambda r: (r["contract_key"], r["season"]))
    return rows
