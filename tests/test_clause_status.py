"""clause_status: clause / none (stated, CapFriendly 2024) / none_likely / unknown per contract-season."""

from types import SimpleNamespace

from nhl_contracts.normalize.clause_status import STATUS_COLUMNS, clause_status
from nhl_contracts.normalize.model import SeasonClaim


def season(key, s, source_id, clause=None):
    return SeasonClaim(claim_id="c", contract_key=key, season=s, cap_hit_usd=None, aav_usd=None,
                       base_salary_usd=None, signing_bonus_usd=None, performance_bonus_usd=None,
                       total_salary_usd=None, minors_salary_usd=None, one_way=None, clause=clause,
                       source_id=source_id, confidence_tier="D")


def contract(key, start, end, claim_ids=""):
    return {"contract_key": key, "start_season": start, "end_season": end, "claim_ids": claim_ids}


def by_season(rows, key):
    return {r["season"]: (r["status"], r["basis"]) for r in rows if r["contract_key"] == key}


def test_one_row_per_season_with_the_published_columns():
    rows = clause_status([contract("k", "2025-26", "2027-28")], [], [], [])
    assert [r["season"] for r in rows] == ["2025-26", "2026-27", "2027-28"]
    assert all(list(r) == STATUS_COLUMNS for r in rows)
    assert all(r["status"] == "unknown" and r["basis"] is None and r["source_ids"] == "" for r in rows)


def test_a_clause_row_covers_its_seasons_and_open_ends_cover_the_contract():
    # Fantilli-like: "15 Team NTC in Yr 5 & 6" of a six-year deal, plus a breakdown for every year
    clauses = [{"contract_key": "k", "kind": "M-NTC", "season_from": "2030-31", "season_to": "2031-32",
                "source_ids": "bsky:post"}]
    seasons = [season("k", s, "bsky:post", "15 team NTC" if s >= "2030-31" else None)
               for s in ("2026-27", "2027-28", "2028-29", "2029-30", "2030-31", "2031-32")]
    rows = clause_status([contract("k", "2026-27", "2031-32")], clauses, seasons, [])
    got = by_season(rows, "k")
    assert got["2030-31"] == got["2031-32"] == ("clause", "source")
    assert got["2026-27"] == ("none_likely", "breakdown_without_clause")
    # a clause row with no season range covers the whole contract
    whole = [{"contract_key": "w", "kind": "NMC", "season_from": None, "season_to": None, "source_ids": "x"}]
    assert set(by_season(clause_status([contract("w", "2024-25", "2026-27")], whole, [], []), "w").values()) == {
        ("clause", "source")}


def test_a_post_saying_no_trade_protection_is_stated_none_with_that_post_as_source():
    claims = [SimpleNamespace(claim_id="c1", source_id="bsky:iafallo", flags=["no_trade_protection"]),
              SimpleNamespace(claim_id="c2", source_id="forge:story", flags=[])]
    rows = clause_status([contract("k", "2025-26", "2027-28", "c1;c2")], [], [], claims)
    assert set(by_season(rows, "k").values()) == {("none", "stated")}
    assert {r["source_ids"] for r in rows} == {"bsky:iafallo"}


def test_a_blank_capfriendly_clause_cell_is_none():
    seasons = [season("k", "2025-26", "wayback:1:/players/x"), season("k", "2026-27", "wayback:1:/players/x")]
    rows = clause_status([contract("k", "2021-22", "2026-27")], [], seasons, [])
    got = by_season(rows, "k")
    assert got["2026-27"] == ("none", "capfriendly_2024")
    assert got["2021-22"] == ("unknown", None)  # no archived row for that season


def test_a_capfriendly_label_without_a_clause_row_is_not_called_none():
    seasons = [season("k", "2026-27", "wayback:1:/players/x", "NMC")]
    assert by_season(clause_status([contract("k", "2026-27", "2026-27")], [], seasons, []), "k") == {
        "2026-27": ("unknown", None)}


def test_precedence_clause_then_stated_then_capfriendly_then_breakdown():
    clauses = [{"contract_key": "k", "kind": "NTC", "season_from": "2026-27", "season_to": "2026-27",
                "source_ids": "a"}]
    claims = [SimpleNamespace(claim_id="c1", source_id="bsky:p", flags=["no_trade_protection"])]
    seasons = [season("k", s, src) for s in ("2026-27", "2027-28") for src in ("wayback:1:/x", "bsky:p")]
    got = by_season(clause_status([contract("k", "2026-27", "2027-28", "c1")], clauses, seasons, claims), "k")
    assert got == {"2026-27": ("clause", "source"), "2027-28": ("none", "stated")}
    got = by_season(clause_status([contract("k", "2026-27", "2027-28")], [], seasons, []), "k")
    assert got == {"2026-27": ("none", "capfriendly_2024"), "2027-28": ("none", "capfriendly_2024")}


def test_bad_season_ranges_give_no_rows():
    assert clause_status([contract("k", None, "2026-27"), contract("j", "2027-28", "2026-27")], [], [], []) == []
