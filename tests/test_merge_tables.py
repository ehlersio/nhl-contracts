"""Cross-source merge of contracts, seasons and clauses (normalize/merge.py)."""

from nhl_contracts.normalize.merge import group_claims, merge_clauses, merge_contracts, merge_seasons
from nhl_contracts.normalize.model import Claim, ClauseClaim, SeasonClaim, claim_id, make_key


def claim(name, team, signed_at, tier, source, *, pid=None, term=None, cap=None, aav=None, total=None,
          start=None, end=None, flags=(), stated=False, ctype="standard"):
    return Claim(
        claim_id=claim_id(source, name, pid), contract_key=make_key(pid, name, team, signed_at),
        player_name=name, player_id=pid, team=team, signed_at=signed_at, contract_type=ctype,
        term_years=term, aav_usd=aav, cap_hit_usd=cap, total_usd=total, nhl_salary_usd=None,
        minors_salary_usd=None, one_way=None, start_season=start, end_season=end, re_signed=False,
        source_id=source, source_url="u", publisher="p", published_at=signed_at, confidence_tier=tier,
        extracted_by="regex", flags=list(flags), signing_date_stated=stated,
    )


def test_archived_row_without_date_joins_contract_by_seasons():
    story = claim("Connor McDavid", "EDM", "2017-07-05", "B", "forge:x", pid=8478402, term=8, aav=12_500_000,
                  total=100_000_000, start="2018-19", end="2025-26")
    archive = claim("Connor McDavid", "EDM", "2024-07-05", "E", "wayback:y", term=8, cap=12_500_000,
                    start="2018-19", end="2025-26", flags=["signing_date_unknown"])
    rows, key_map = merge_contracts([story, archive])
    assert len(rows) == 1
    r = rows[0]
    assert r["player_id"] == 8478402 and r["n_sources"] == 2 and r["tiers"] == "BE"
    assert r["signed_at"] == "2017-07-05" and r["conflicts"] == ""
    assert key_map[story.claim_id] == key_map[archive.claim_id] == r["contract_key"]


def test_stated_signing_date_versus_publication_date():
    release = claim("Marat Khusnutdinov", "BOS", "2026-10-21", "A", "forge:a", term=3, cap=3_950_000,
                    start="2027-28", end="2029-30", ctype="extension")
    post = claim("Marat Khusnutdinov", "BOS", "2026-10-22", "D", "bsky:b", term=3, cap=3_950_000,
                 start="2027-28", end="2029-30", ctype="extension")
    rows, _ = merge_contracts([release, post])
    assert len(rows) == 1 and rows[0]["best_tier"] == "A" and rows[0]["cap_hit_usd"] == 3_950_000


def test_different_contracts_stay_apart():
    elc = claim("Connor McDavid", "EDM", "2015-07-03", "E", "wayback:1", term=3, start="2015-16", end="2017-18",
                stated=True, ctype="ELC")
    ext = claim("Connor McDavid", "EDM", "2017-07-05", "E", "wayback:1", term=8, start="2018-19", end="2025-26",
                stated=True, ctype="extension")
    assert len(group_claims([elc, ext])) == 2


def test_season_and_clause_merge_report_conflicts():
    a = claim("Adam Fantilli", "CBJ", "2026-09-24", "A", "forge:f", term=6, cap=13_750_000)
    d = claim("Adam Fantilli", "CBJ", "2026-09-24", "D", "bsky:f", term=6, cap=13_750_000)
    rows, key_map = merge_contracts([a, d])
    key = rows[0]["contract_key"]
    seasons = [
        SeasonClaim(a.claim_id, a.contract_key, "2026-27", 13_750_000, None, 6_000_000, 10_000_000, None,
                    16_000_000, None, None, None, "forge:f", "A"),
        SeasonClaim(d.claim_id, d.contract_key, "2026-27", 13_750_000, None, 6_000_000, 9_000_000, None,
                    15_000_000, None, None, None, "bsky:f", "D"),
        SeasonClaim(d.claim_id, d.contract_key, "2027-28", 13_750_000, None, 5_000_000, 10_000_000, None,
                    15_000_000, None, None, None, "bsky:f", "D"),
    ]
    merged = merge_seasons(seasons, key_map)
    assert [m["season"] for m in merged] == ["2026-27", "2027-28"]
    first = merged[0]
    assert first["contract_key"] == key and first["base_salary_usd"] == 6_000_000
    assert first["signing_bonus_usd"] is None and "signing_bonus_usd" in first["conflicts"]
    assert first["n_sources"] == 2 and first["best_tier"] == "A"
    clauses = [ClauseClaim(d.claim_id, d.contract_key, "M-NTC", "2030-31", "2031-32", 15, None, "bsky:f", "D")]
    cz = merge_clauses(clauses, key_map)
    assert cz == [{"contract_key": key, "kind": "M-NTC", "season_from": "2030-31", "season_to": "2031-32",
                   "list_size": 15, "list_type": None, "conflicts": "", "n_sources": 1, "best_tier": "D",
                   "source_ids": "bsky:f"}]


def test_surname_only_post_joins_player_on_same_team_and_window():
    release = claim("Adam Lowry", "WPG", "2026-07-01", "A", "forge:l", pid=8476392, term=5, aav=5_000_000)
    post = claim("Lowry", "WPG", "2026-07-02", "D", "bsky:l", term=5, cap=5_000_000, flags=["surname_only"])
    other = claim("Lowry", "WPG", "2026-10-02", "D", "bsky:l2", term=1, cap=850_000, flags=["surname_only"])
    rows, _ = merge_contracts([release, post, other])
    assert len(rows) == 2
    merged = next(r for r in rows if r["n_sources"] == 2)
    assert merged["player_id"] == 8476392 and merged["tiers"] == "AD"


def test_assign_player_ids_from_roster():
    from nhl_contracts.normalize.merge import assign_player_ids

    a = claim("Juuso Valimaki", "CAR", "2026-06-24", "E", "wayback:v")
    b = claim("Sebastian Aho", "CAR", "2026-06-24", "E", "wayback:a")
    c = claim("Sebastian Aho", "NYI", "2026-06-24", "E", "wayback:a2")
    players = {1: ("Juuso Välimäki", "CAR"), 2: ("Sebastian Aho", "CAR"), 3: ("Sebastian Aho", "NYI")}
    assert assign_player_ids([a, b, c], players) == 3
    assert (a.player_id, b.player_id, c.player_id) == (1, 2, 3)
    assert "player_id_from_roster" in a.flags


def test_assumed_start_season_yields_to_stated_one():
    club = claim("Jack Anderson", "DAL", "2026-03-18", "A", "forge:j", term=2, start="2026-27", end="2027-28",
                 ctype="ELC")
    post = claim("Jack Anderson", "DAL", "2026-03-18", "D", "bsky:j", term=2, cap=925_000, start="2025-26",
                 end="2026-27", ctype="ELC", flags=["start_season_assumed"])
    rows, _ = merge_contracts([club, post])
    assert len(rows) == 1 and rows[0]["conflicts"] == ""
    assert (rows[0]["start_season"], rows[0]["end_season"]) == ("2026-27", "2027-28")
    only_post, _ = merge_contracts([post])
    assert only_post[0]["start_season"] == "2025-26"


def test_alias_matching_and_grouping():
    from nhl_contracts.normalize.merge import assign_player_ids
    from nhl_contracts.normalize.text import alias_key

    assert alias_key("Zachary Werenski") == alias_key("Zach Werenski") == "zachary werenski"
    assert alias_key("A.J. Greer") == "aj greer" and alias_key("Jean-Gabriel Pageau") == "jeangabriel pageau"
    a = claim("Zachary Werenski", "CBJ", "2024-07-05", "E", "wayback:w", term=6, cap=9_583_333,
              start="2022-23", end="2027-28", flags=["signing_date_unknown"])
    b = claim("Zach Werenski", "CBJ", "2022-07-01", "C", "espn:w", term=6, start="2022-23", end="2027-28")
    assert assign_player_ids([a, b], {8478460: ("Zach Werenski", "CBJ")}) == 2
    assert a.player_id == 8478460 and "player_id_by_alias" in a.flags and "player_id_from_roster" in b.flags
    rows, _ = merge_contracts([a, b])
    assert len(rows) == 1 and rows[0]["tiers"] == "CE"


def test_span_inferred_from_signing_date_is_flagged():
    espn = claim("Sidney Crosby", "PIT", "2024-09-16", "C", "espn:c", pid=8471675, term=2, ctype="extension")
    rows, _ = merge_contracts([espn])
    r = rows[0]
    assert (r["start_season"], r["end_season"]) == ("2025-26", "2026-27")
    assert "derived:span_from_signing_date" in r["flags"]
    july = claim("Kevin Stenlund", "UTA", "2026-07-01", "A", "forge:s", term=1)
    assert merge_contracts([july])[0][0]["start_season"] == "2026-27"
    stated = claim("Marat Khusnutdinov", "BOS", "2026-10-21", "A", "forge:k", term=3, start="2027-28", end="2029-30")
    assert "derived:span_from_signing_date" not in merge_contracts([stated])[0][0]["flags"]
