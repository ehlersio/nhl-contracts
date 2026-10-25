"""A buyout ends the contract it buys out.

``dead_cap`` carries a buyout's charges, one row per season, the first being the
season the buyout takes effect (a June 2025 buyout charges 2025-26 first). The
contract's stated span still runs to the season it was signed through, so on its
own ``contracts`` would show a bought-out deal as live for years after the
player left. :func:`apply_buyouts` cuts each bought-out contract to the season
before the first charge, keeps the signed end in ``end_season_signed``, and drops
the contract's per-season rows after the new end (those seasons are never paid
under the contract; the dead-cap rows hold the charges).

Two limits of the sources shape the rule. A deal that starts in or after the
first charged season with another team is the player's next contract, not the
one bought out, and is left alone. And the archived CapFriendly team pages
(tier E) show charges from 2024-25 on only, so for a buyout known from them alone
the first charge seen may be later than the real one: the cut is then an upper
bound, flagged ``buyout_season_upper_bound``.
"""

from __future__ import annotations

from typing import Any

from .text import name_key, shift_season

TIER_RANK = {t: i for i, t in enumerate("ABCDE")}


def _add_flags(row: dict[str, Any], *flags: str) -> None:
    row["flags"] = ";".join(sorted(set(filter(None, (row.get("flags") or "").split(";"))) | set(flags)))


def _nk(name: str) -> str:
    # "Marc-Édouard Vlasic" on the contract, "Marc Edouard Vlasic" from a buyout link
    return name_key(name.replace("-", " "))


def _signed_end(r: dict[str, Any]) -> str | None:
    return r.get("end_season_signed") or r.get("end_season")


def apply_buyouts(contracts: list[dict[str, Any]], seasons: list[dict[str, Any]],
                  dead_cap_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Terminate every merged contract a ``buyout`` dead-cap row buys out.

    The player is matched by NHL id when both rows have one, else by normalized
    name; the contract is the one whose stated span covers the buyout's first
    charged season, preferring the buyout team's rows (a player can have an
    id-keyed and a name-keyed row for one deal: all of them are cut). The inputs
    are not modified."""
    contracts = [dict(r) for r in contracts]
    dead_cap_rows = [dict(r) for r in dead_cap_rows]

    # one buyout per (team, player): its rows and its first charged season
    groups: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for d in dead_cap_rows:
        if d.get("kind") == "buyout" and d.get("season") and d.get("player_name"):
            groups.setdefault((d.get("team"), _nk(d["player_name"])), []).append(d)

    by_id: dict[int, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in contracts:
        if not (r.get("start_season") and _signed_end(r)):
            continue
        if r.get("player_id"):
            by_id.setdefault(r["player_id"], []).append(r)
        by_name.setdefault(_nk(r.get("player_name") or ""), []).append(r)

    # contract row id -> (first charged season, team mismatch, archive-only first season)
    hits: dict[int, tuple[str, bool, bool]] = {}
    for (team, nk), rows in groups.items():
        first = min(d["season"] for d in rows)
        archived_only = all(d.get("best_tier") == "E" for d in rows if d["season"] == first)
        pid = next((d["player_id"] for d in rows if d.get("player_id")), None)
        if pid:
            cands = by_id.get(pid, []) + [r for r in by_name.get(nk, []) if not r.get("player_id")]
        else:
            cands = list(by_name.get(nk, []))
        covering = [r for r in cands if r["start_season"] <= first <= _signed_end(r)]
        same_team = [r for r in covering if team and r.get("team") == team]
        # another team's deal starting at or after the first charge is the next contract
        matched = same_team or [r for r in covering if r["start_season"] < first]
        if not matched:
            continue
        for r in matched:
            prev = hits.get(id(r))
            if prev is None or first < prev[0]:
                hits[id(r)] = (first, not same_team, archived_only)
        best = min(matched, key=lambda r: (TIER_RANK.get(r.get("best_tier"), 9), r["contract_key"]))
        for d in rows:
            if not d.get("contract_key"):
                d["contract_key"] = best["contract_key"]

    new_end: dict[str, str] = {}
    n_bought_out = n_before_start = 0
    for r in contracts:
        hit = hits.get(id(r))
        if hit is None:
            r["bought_out"] = False
            r["end_season_signed"] = None
            continue
        first, mismatch, archived_only = hit
        signed = _signed_end(r)
        r["bought_out"] = True
        r["end_season_signed"] = signed
        n_bought_out += 1
        extra = ("bought_out_team_mismatch",) if mismatch else ()
        if first <= r["start_season"]:
            r["end_season"] = signed
            _add_flags(r, "bought_out_before_start", *extra)
            n_before_start += 1
            continue
        r["end_season"] = shift_season(first, -1)
        if archived_only:
            extra += ("buyout_season_upper_bound",)
        _add_flags(r, "bought_out", "derived:end_season_from_buyout", *extra)
        new_end[r["contract_key"]] = r["end_season"]

    kept = [s for s in seasons if not (s.get("contract_key") in new_end and s["season"] > new_end[s["contract_key"]])]
    return {"contracts": contracts, "seasons": kept, "dead_cap": dead_cap_rows, "n_bought_out": n_bought_out,
            "n_bought_out_before_start": n_before_start, "n_seasons_dropped": len(seasons) - len(kept)}
