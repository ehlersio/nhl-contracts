"""Merge per-source rows into one row per contract, per contract-season and per clause.

Nothing is resolved by preference: when two sources disagree on a field the
merged value is null and the field name is listed in ``conflicts``. The
per-source rows (claims, season_claims, clause_claims) stay published next to
the merged tables so a consumer can always see who said what.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .model import TIER_ORDER, Claim, ClauseClaim, SeasonClaim
from .text import name_key

MERGE_FIELDS = ("contract_type", "term_years", "aav_usd", "cap_hit_usd", "total_usd",
                "nhl_salary_usd", "minors_salary_usd", "one_way", "start_season", "end_season",
                "expiry_status", "agent")
SEASON_FIELDS = ("cap_hit_usd", "aav_usd", "base_salary_usd", "signing_bonus_usd",
                 "performance_bonus_usd", "total_salary_usd", "minors_salary_usd", "one_way", "clause")
WINDOW_DAYS = 45
# A value carried with one of these flags is a guess by the parser, not a statement by
# the source: it yields to any stated value instead of conflicting with it.
ASSUMED = {
    "start_season": ("start_season_assumed", "derived:season_from_signing_date", "derived:start_next_season"),
    "end_season": ("start_season_assumed", "derived:season_from_signing_date", "derived:start_next_season",
                   "term_is_remaining_seasons"),
    "term_years": ("term_is_remaining_seasons", "derived:term_from_seasons"),
    "total_usd": ("total_includes_performance_bonuses", "derived:total_from_seasons", "derived:total_one_year"),
    "aav_usd": ("derived:aav_one_year", "derived:aav_from_nhl_salary", "derived:all_salary_from_cap_hit"),
}


def _stated(claims: list[Claim], field_name: str) -> list[Claim]:
    """Claims whose value for ``field_name`` is stated rather than assumed, when any are."""
    flags = ASSUMED.get(field_name, ())
    stated = [c for c in claims if getattr(c, field_name) is not None
              and not any(f in c.flags for f in flags)]
    return stated or [c for c in claims if getattr(c, field_name) is not None]


def _tier_rank(t: str) -> int:
    return TIER_ORDER.index(t) if t in TIER_ORDER else len(TIER_ORDER)


def _seasons_overlap(a: Claim, b: Claim) -> bool:
    if not (a.start_season and a.end_season and b.start_season and b.end_season):
        return False
    return a.start_season <= b.end_season and b.start_season <= a.end_season


def _same_contract(c: Claim, g0: Claim) -> bool:
    same_team = c.team is None or g0.team is None or c.team == g0.team
    if not same_team:
        return False
    pto = "PTO" in (c.contract_type, g0.contract_type)
    if pto and c.contract_type != g0.contract_type:
        return False
    days = abs((date.fromisoformat(c.signed_at) - date.fromisoformat(g0.signed_at)).days)
    if days <= WINDOW_DAYS:
        return True
    # An archived row whose signing date is unknown (dated by its snapshot) joins a
    # contract that covers the same seasons.
    undated = "signing_date_unknown" in c.flags or "signing_date_unknown" in g0.flags
    if undated and _seasons_overlap(c, g0):
        term_ok = c.term_years is None or g0.term_years is None or c.term_years == g0.term_years
        return term_ok
    # A stated signing date (archive) versus a publication date (story) for the same deal
    stated = c.signing_date_stated != g0.signing_date_stated
    if stated and _seasons_overlap(c, g0) and c.start_season == g0.start_season:
        term_ok = c.term_years is None or g0.term_years is None or c.term_years == g0.term_years
        return term_ok and days <= 400
    return False


def assign_player_ids(claims: list[Claim], players: dict[int, tuple[str, str | None]]) -> int:
    """Fill ``player_id`` on claims that lack one when exactly one known player has the
    same normalized name (accents folded). Returns the number of claims filled. A
    name shared by two players is left alone unless the team disambiguates it."""
    by_name: dict[str, list[tuple[int, str | None]]] = {}
    for pid, (name, team) in players.items():
        by_name.setdefault(name_key(name), []).append((pid, team))
    filled = 0
    for c in claims:
        if c.player_id:
            continue
        cands = by_name.get(name_key(c.player_name), [])
        if len(cands) > 1 and c.team:
            cands = [x for x in cands if x[1] == c.team] or cands
        if len(cands) == 1:
            c.player_id = cands[0][0]
            c.flags.append("player_id_from_roster")
            filled += 1
    return filled


def _surname_key_lookup(who: str, c: Claim, by_player: dict[str, list[Claim]]) -> str:
    """A surname-only claim ("Lowry", flag surname_only) joins the player whose key ends
    with that surname and who has a claim on the same team within the signing window."""
    surname = who.split()[-1]
    hits = []
    for key, cs in by_player.items():
        if key.split()[-1] != surname or key == who:
            continue
        for other in cs:
            if other.team == c.team and abs(
                (date.fromisoformat(other.signed_at) - date.fromisoformat(c.signed_at)).days
            ) <= WINDOW_DAYS:
                hits.append(key)
                break
    return hits[0] if len(hits) == 1 else who


def group_claims(claims: list[Claim]) -> list[list[Claim]]:
    """Group claims that describe the same contract: same player (NHL id when both
    have one, else normalized name), same team, and the same signing window or the
    same covered seasons."""
    id_to_name: dict[int, str] = {}
    for c in claims:
        if c.player_id:
            id_to_name.setdefault(c.player_id, name_key(c.player_name))
    name_to_id: dict[str, int] = {v: k for k, v in id_to_name.items()}
    by_player: dict[str, list[Claim]] = {}
    deferred: list[Claim] = []
    for c in claims:
        who = name_key(c.player_name)
        if c.player_id:
            who = id_to_name[c.player_id]
        elif who in name_to_id:
            who = id_to_name[name_to_id[who]]
        elif "surname_only" in c.flags:
            deferred.append(c)
            continue
        by_player.setdefault(who, []).append(c)
    for c in deferred:
        who = _surname_key_lookup(name_key(c.player_name), c, by_player)
        by_player.setdefault(who, []).append(c)
    groups: list[list[Claim]] = []
    for cs in by_player.values():
        cs.sort(key=lambda c: (not c.signing_date_stated, c.signed_at))
        open_groups: list[list[Claim]] = []
        for c in cs:
            for g in open_groups:
                if any(_same_contract(c, other) for other in g):
                    g.append(c)
                    break
            else:
                open_groups.append([c])
        groups.extend(open_groups)
    return groups


def _consensus(values: set[Any]) -> tuple[Any, bool]:
    if len(values) == 1:
        return values.pop(), False
    if len(values) > 1:
        return None, True
    return None, False


def merge_contracts(claims: list[Claim]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return merged contract rows and a map claim_id -> merged contract_key."""
    rows = []
    key_map: dict[str, str] = {}
    for g in group_claims(claims):
        g.sort(key=lambda c: (_tier_rank(c.confidence_tier), not c.signing_date_stated, c.signed_at))
        best = g[0]
        dated = [c for c in g if c.signing_date_stated] or g
        row: dict[str, Any] = {
            "contract_key": best.contract_key,
            "player_name": best.player_name,
            "player_id": next((c.player_id for c in g if c.player_id), None),
            "team": next((c.team for c in g if c.team), None),
            "signed_at": min(c.signed_at for c in dated),
            "signing_date_stated": any(c.signing_date_stated for c in g),
        }
        conflicts = []
        for f in MERGE_FIELDS:
            vals = {getattr(c, f) for c in _stated(g, f)}
            if f == "contract_type":
                vals = {v for v in vals if v != "standard"} or vals
            value, conflict = _consensus(vals)
            row[f] = value
            if conflict:
                conflicts.append(f)
        row["re_signed"] = any(c.re_signed for c in g)
        row["conflicts"] = ";".join(conflicts)
        row["n_sources"] = len({c.source_id for c in g})
        row["best_tier"] = best.confidence_tier
        row["tiers"] = "".join(sorted({c.confidence_tier for c in g}))
        row["source_ids"] = ";".join(sorted({c.source_id for c in g}))
        row["claim_ids"] = ";".join(c.claim_id for c in g)
        row["flags"] = ";".join(sorted({f for c in g for f in c.flags if not f.startswith("ref:")}))
        for c in g:
            c.contract_key = row["contract_key"]
            key_map[c.claim_id] = row["contract_key"]
        rows.append(row)
    rows.sort(key=lambda r: (r["signed_at"], r["team"] or "", r["player_name"]))
    return rows, key_map


def merge_seasons(seasons: list[SeasonClaim], key_map: dict[str, str]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], list[SeasonClaim]] = {}
    for s in seasons:
        s.contract_key = key_map.get(s.claim_id, s.contract_key)
        by_key.setdefault((s.contract_key, s.season), []).append(s)
    rows = []
    for (ckey, season), ss in by_key.items():
        ss.sort(key=lambda s: _tier_rank(s.confidence_tier))
        row: dict[str, Any] = {"contract_key": ckey, "season": season}
        conflicts = []
        for f in SEASON_FIELDS:
            value, conflict = _consensus({getattr(s, f) for s in ss if getattr(s, f) is not None})
            row[f] = value
            if conflict:
                conflicts.append(f)
        row["conflicts"] = ";".join(conflicts)
        row["n_sources"] = len({s.source_id for s in ss})
        row["best_tier"] = ss[0].confidence_tier
        row["source_ids"] = ";".join(sorted({s.source_id for s in ss}))
        rows.append(row)
    rows.sort(key=lambda r: (r["contract_key"], r["season"]))
    return rows


def merge_clauses(clauses: list[ClauseClaim], key_map: dict[str, str]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str | None, str | None], list[ClauseClaim]] = {}
    for c in clauses:
        c.contract_key = key_map.get(c.claim_id, c.contract_key)
        by_key.setdefault((c.contract_key, c.kind, c.season_from, c.season_to), []).append(c)
    rows = []
    for (ckey, kind, s_from, s_to), cs in by_key.items():
        cs.sort(key=lambda c: _tier_rank(c.confidence_tier))
        sizes = {c.list_size for c in cs if c.list_size is not None}
        types = {c.list_type for c in cs if c.list_type}
        size, size_conflict = _consensus(set(sizes))
        ltype, type_conflict = _consensus(set(types))
        rows.append({
            "contract_key": ckey, "kind": kind, "season_from": s_from, "season_to": s_to,
            "list_size": size, "list_type": ltype,
            "conflicts": ";".join([f for f, c in (("list_size", size_conflict), ("list_type", type_conflict)) if c]),
            "n_sources": len({c.source_id for c in cs}), "best_tier": cs[0].confidence_tier,
            "source_ids": ";".join(sorted({c.source_id for c in cs})),
        })
    rows.sort(key=lambda r: (r["contract_key"], r["season_from"] or "", r["kind"]))
    return rows
