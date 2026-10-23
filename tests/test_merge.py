from conftest import load_story

from nhl_contracts.normalize.contracts import extract_story, merge, normalize_all


def test_club_and_staff_story_merge_without_conflict(teams):
    a, _ = extract_story(load_story("bruins-sign-marat-khusnutdinov-to-three-year-contract-extension"), teams)
    b, _ = extract_story(load_story("marat-khusnutdinov-signs-three-year-contract-with-boston-bruins"), teams)
    rows = merge(a + b)
    assert len(rows) == 1
    r = rows[0]
    assert r["n_sources"] == 2 and r["best_tier"] == "A" and r["conflicts"] == ""
    assert (r["term_years"], r["aav_usd"], r["cap_hit_usd"], r["total_usd"]) == (3, 3_950_000, 3_950_000, 11_850_000)
    assert r["player_id"] == 8482177 and r["contract_type"] == "extension"
    assert a[0].contract_key == b[0].contract_key == r["contract_key"]


def test_conflicting_values_are_kept_not_resolved(teams):
    a, _ = extract_story(load_story("bruins-sign-marat-khusnutdinov-to-three-year-contract-extension"), teams)
    b, _ = extract_story(load_story("marat-khusnutdinov-signs-three-year-contract-with-boston-bruins"), teams)
    b[0].term_years = 4  # pretend the staff story disagreed
    r = merge(a + b)[0]
    assert r["term_years"] is None and r["conflicts"] == "term_years"
    assert len(r["claim_ids"].split(";")) == 2


def test_two_players_in_one_story_are_two_contracts(teams):
    claims, _ = extract_story(load_story("tyler-myers-agrees-to-3-year-contract-with-canucks"), teams)
    rows = merge(claims)
    assert {r["player_name"] for r in rows} == {"Tyler Myers", "Dakota Joshua"}
    assert sum(1 for r in rows if r["player_id"] == 8474574) == 1


def test_normalize_all_end_to_end(stories, espn_payload, teams):
    espn_rows = [{"key": str(i), "date": t["date"], "team_abbrev": t["team"]["abbreviation"],
                  "description": t["description"]} for i, t in enumerate(espn_payload["transactions"])]
    claims, sources, merged = normalize_all(stories, espn_rows, teams)
    assert len(merged) < len(claims)
    assert {s.kind for s in sources} == {"nhl_forge", "espn_transactions"}
    # an ESPN row and a club release about the same signing land in one contract row
    hits = [r for r in merged if r["player_name"] == "Cole Perfetti"]
    assert len(hits) == 1 and hits[0]["n_sources"] >= 2 and hits[0]["term_years"] == 5
