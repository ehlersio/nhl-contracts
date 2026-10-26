"""Per-season rows read from official releases (real Forge fixtures)."""

from conftest import load_story

from nhl_contracts.normalize.contracts import extract_story, extract_story_seasons, normalize_all

NO_SPLIT = dict(base_salary_usd=None, signing_bonus_usd=None, performance_bonus_usd=None)


def seasons_for(slug, teams):
    story = load_story(slug)
    claims, _ = extract_story(story, teams)
    rows = extract_story_seasons(story, claims, teams)
    return claims, rows


def one_claim(slug, teams):
    claims, rows = seasons_for(slug, teams)
    assert len(claims) == 1, [c.player_name for c in claims]
    return claims[0], {r.season: r for r in rows}


def fields(row, *names):
    return tuple(getattr(row, n) for n in names)


def test_hischier_schedule_rows_link_to_the_claim(teams):
    c, rows = one_claim("hischier-agrees-to-terms-on-contract-extension-release", teams)
    assert [(s, r.total_salary_usd) for s, r in rows.items()] == [
        ("2027-28", 14_500_000), ("2028-29", 14_500_000), ("2029-30", 12_100_000),
        ("2030-31", 8_700_000), ("2031-32", 8_700_000)]
    for r in rows.values():
        assert fields(r, *NO_SPLIT) == (None, None, None)  # the club does not split base and bonus
        assert r.cap_hit_usd == r.aav_usd == 11_700_000
        assert (r.claim_id, r.contract_key, r.source_id, r.confidence_tier) == (
            c.claim_id, c.contract_key, c.source_id, "A")
        assert "2027-28: $14,500,000" in r.evidence
    # the stated total is a club typo ($58,500,00); the season sum disagrees and is flagged, not fixed
    assert c.total_usd == 5_850_000 and "season_sum_mismatch" in c.flags


def test_evangelista_and_daws_schedules(teams):
    c, rows = one_claim("evangelista-extension-release", teams)
    assert [r.total_salary_usd for r in rows.values()] == [9_700_000, 8_260_000, 6_120_000, 6_085_000, 6_085_000]
    assert sum(r.total_salary_usd for r in rows.values()) == c.total_usd and "season_sum_mismatch" not in c.flags
    _, rows = one_claim("daws-re-sign", teams)
    assert {s: r.total_salary_usd for s, r in rows.items()} == {"2026-27": 975_000, "2027-28": 1_225_000}


def test_mantha_amount_without_dollar_sign_is_read_and_flagged(teams):
    c, rows = one_claim("mantha-joins-devils", teams)
    assert {s: r.total_salary_usd for s, r in rows.items()} == {"2026-27": 5_400_000, "2027-28": 4_100_000}
    assert "breakdown_missing_dollar_sign" in c.flags
    # claim level: "$9,500,000, with an annual average value of $4,750,000" is total then AAV
    assert (c.total_usd, c.aav_usd) == (9_500_000, 4_750_000)


def test_lombardi_mixed_two_way_then_one_way(teams):
    c, rows = one_claim("lombardi-signing", teams)
    assert fields(rows["2026-27"], "base_salary_usd", "minors_salary_usd", "one_way") == (850_000, 175_000, False)
    assert fields(rows["2027-28"], "base_salary_usd", "minors_salary_usd", "one_way") == (900_000, None, True)
    assert c.total_usd is None  # "$900,000" is the second season's salary, not the total


def test_malek_slash_pair_and_guarantee(teams):
    c, rows = one_claim("malek-re-signs", teams)
    assert fields(rows["2027-28"], "base_salary_usd", "minors_salary_usd", "one_way") == (900_000, 275_000, False)
    assert fields(rows["2026-27"], "base_salary_usd", "minors_salary_usd") == (850_000, 175_000)
    assert "guaranteed:300000" in c.flags
    assert all(r.minors_salary_usd != 300_000 for r in rows.values())


def test_devils_one_year_details_blocks(teams):
    for slug, minors, guaranteed in (("devils-re-sign-vilen", 150_000, "guaranteed:175000"),
                                     ("mclaughlin-re-signs", 350_000, None),
                                     ("steeves-re-signed", 150_000, None),
                                     ("devils-sign-white", 400_000, None)):
        c, rows = one_claim(slug, teams)
        assert list(rows) == ["2026-27"], slug
        r = rows["2026-27"]
        assert fields(r, "base_salary_usd", "minors_salary_usd", "one_way") == (
            850_000 if slug != "devils-sign-white" else 900_000, minors, False), slug
        if guaranteed:
            assert guaranteed in c.flags


def test_parent_season_label_kept_as_written_and_flagged(teams):
    c, rows = one_claim("parent-re-signs", teams)
    assert list(rows) == ["2025-26"]  # the release says 2025-26 for a deal published 2026-07-01
    assert "season_breakdown_mismatch" in c.flags and "guaranteed:200000" in c.flags


def test_kochetkov_salary_plus_signing_bonus(teams):
    c, rows = one_claim("canes-sign-kochetkov", teams)
    assert fields(rows["2027-28"], "base_salary_usd", "signing_bonus_usd", "total_salary_usd") == (
        2_400_000, 1_500_000, 3_900_000)
    assert fields(rows["2028-29"], "base_salary_usd", "signing_bonus_usd", "total_salary_usd") == (
        3_000_000, 1_000_000, 4_000_000)
    assert c.aav_usd is None and c.cap_hit_usd is None and c.total_usd is None


def test_staal_base_signing_and_performance_bonus(teams):
    c, rows = one_claim("canes-sign-staal", teams)
    assert list(rows) == [c.start_season] == ["2027-28"]
    r = rows["2027-28"]
    assert fields(r, "base_salary_usd", "signing_bonus_usd", "performance_bonus_usd", "total_salary_usd") == (
        900_000, 4_000_000, 5_100_000, 4_900_000)
    assert c.aav_usd is None


def test_giroux_base_salary_and_unclear_bonus_maximum(teams):
    c, rows = one_claim("senators-agree-to-one-year-contract-with-claude-giroux", teams)
    r = rows["2026-27"]  # from the headline "for 2026-27 season"
    assert r.base_salary_usd == 2_000_000 and r.performance_bonus_usd is None
    assert "bonus_unclear:5000000" in c.flags


def test_hakansson_elc_nhl_schedule_and_minors_per_season(teams):
    c, rows = one_claim("canes-sign-hakansson", teams)
    assert {s: (r.base_salary_usd, r.minors_salary_usd) for s, r in rows.items()} == {
        "2026-27": (850_000, 87_500), "2027-28": (900_000, 87_500), "2028-29": (950_000, 87_500)}
    assert all(r.signing_bonus_usd is None for r in rows.values())  # a contract-long total is not split
    assert "signing_bonus_total:288000" in c.flags
    assert c.total_usd is None and c.nhl_salary_usd == 850_000


def test_one_year_two_way_pairs(teams):
    for slug, base, minors in (("oskar-sundqvist-signs-1-year", 850_000, 300_000),
                               ("utah-signs-joshua-roy", 850_000, None),
                               ("capitals-sign-jacob-macdonald", 850_000, 525_000),
                               ("capitals-sign-forward-theodor-niederbach", 1_025_000, 85_000),
                               ("canes-sign-heimosalmi", 850_000, 85_000)):
        c, rows = one_claim(slug, teams)
        assert list(rows) == ["2026-27"], slug
        r = rows["2026-27"]
        assert fields(r, "base_salary_usd", "minors_salary_usd", "one_way") == (base, minors, False), slug
        assert "derived:season_from_signing_date" in c.flags, slug  # the season is not in the text


def test_seeley_guarantee_and_stated_season(teams):
    c, rows = one_claim("canes-sign-seeley", teams)
    assert fields(rows["2026-27"], "base_salary_usd", "minors_salary_usd") == (850_000, 100_000)
    assert "guaranteed:160000" in c.flags and "derived:season_from_signing_date" not in c.flags


def test_minnesota_per_player_parentheticals(teams):
    claims, rows = seasons_for("minnesota-wild-re-signs-forward-caedan-bankier", teams)
    by_id = {c.claim_id: c.player_name for c in claims}
    got = {by_id[r.claim_id]: (r.season, r.base_salary_usd, r.minors_salary_usd) for r in rows}
    assert got == {"Caedan Bankier": ("2026-27", 850_000, 100_000),
                   "Carson Lambos": ("2026-27", 850_000, 110_000),
                   "David Spacek": ("2026-27", 850_000, 110_000)}


def test_hyry_first_year_two_way_second_year_one_way(teams):
    claims, rows = seasons_for("dallas-stars-sign-forwards-arttu-hyry-and-artem-shlaine", teams)
    hyry = next(c for c in claims if c.player_name == "Arttu Hyry")
    assert hyry.total_usd is None  # "one-way $900,000 contract" is the second season, not the total
    mine = {r.season: r for r in rows if r.claim_id == hyry.claim_id}
    assert fields(mine["2026-27"], "one_way", "base_salary_usd") == (False, None)
    assert fields(mine["2027-28"], "one_way", "base_salary_usd") == (True, 900_000)
    assert all(r.claim_id == hyry.claim_id for r in rows)  # nothing bleeds to Shlaine or Halverson


def test_capobianco_first_and_second_year(teams):
    claims, rows = seasons_for("dallas-stars-sign-defenseman-kyle-capobianco", teams)
    cap = next(c for c in claims if c.player_name == "Kyle Capobianco")
    assert cap.total_usd is None
    assert {r.season: (r.total_salary_usd, r.one_way) for r in rows} == {
        "2026-27": (850_000, True), "2027-28": (900_000, True)}


def test_ovechkin_salary_bonuses(teams):
    c, rows = one_claim("capitals-re-sign-alex-ovechkin", teams)
    r = rows["2026-27"]
    assert fields(r, "base_salary_usd", "signing_bonus_usd", "performance_bonus_usd", "total_salary_usd") == (
        1_000_000, 3_250_000, 4_750_000, 4_250_000)
    assert c.aav_usd == 4_250_000 and c.total_usd == 4_250_000  # "$1 million in salary" is no longer the total


def test_no_rows_without_a_breakdown(teams):
    for slug in ("capitals-sign-oliver-suvanto",           # AAV at NHL level over three years
                 "utah-signs-zac-jones",                   # AAV only
                 "jets-sign-defenceman-henry-thrun",       # one-year, AAV only
                 "david-rittich-signs",                    # one-year, one-way, total only
                 "marat-khusnutdinov-signs-three-year-contract-with-boston-bruins",
                 "boone-jenner-signs-4-year"):             # Ovechkin's one-year terms quoted in a Jenner story
        _, rows = seasons_for(slug, teams)
        assert rows == [], slug


def test_flags_are_idempotent(teams):
    story = load_story("malek-re-signs")
    claims, _ = extract_story(story, teams)
    first = extract_story_seasons(story, claims, teams)
    flags = list(claims[0].flags)
    assert extract_story_seasons(story, claims, teams) == first and claims[0].flags == flags


def test_normalize_all_returns_season_rows_keyed_to_contracts(stories, espn_payload, teams):
    espn_rows = [{"key": str(i), "date": t["date"], "team_abbrev": t["team"]["abbreviation"],
                  "description": t["description"]} for i, t in enumerate(espn_payload["transactions"])]
    claims, _sources, merged, seasons = normalize_all(stories, espn_rows, teams)
    assert len(seasons) >= 45
    assert all(s.source_id.startswith("forge:") for s in seasons)  # nothing from ESPN
    keys = {r["contract_key"] for r in merged}
    by_claim = {c.claim_id: c for c in claims}
    for s in seasons:
        assert s.contract_key in keys and by_claim[s.claim_id].contract_key == s.contract_key
    assert len({(s.claim_id, s.season) for s in seasons}) == len(seasons)


def test_giroux_bonus_ceiling_is_not_the_contract_value(teams):
    c, _ = one_claim("senators-agree-to-one-year-contract-with-claude-giroux", teams)
    # "a base salary of $2 million ... with bonuses that could reach a maximum value of $5 million":
    # the release states the base and the ceiling with bonuses, not an AAV or cap hit
    assert (c.nhl_salary_usd, c.total_usd, c.aav_usd, c.cap_hit_usd) == (2_000_000, 5_000_000, None, None)
    assert "total_includes_performance_bonuses" in c.flags and "bonus_unclear:5000000" in c.flags


def test_bonus_ceiling_from_a_later_sentence_is_not_an_aav(teams):
    claims, _ = seasons_for("claude-giroux-signs-1-year-contract-to-remain-with-ottawa", teams)
    (c,) = claims
    # "It has a base salary of $2 million with bonuses that could reach a maximum value of $4.75 million."
    assert (c.nhl_salary_usd, c.total_usd, c.aav_usd) == (2_000_000, 4_750_000, None)


def test_no_aav_from_seasons_that_disagree_with_the_total(teams):
    claims, _ = seasons_for("blues-sign-tucker-to-two-year-extension", teams)
    (c,) = claims
    # "$1.85 million ($900,000 in 2025-26, $950,000 in 2026-27)"
    assert c.total_usd == 1_850_000 and "derived:aav_from_season_salaries" not in c.flags


def test_ellis_labelled_nhl_and_ahl_halves(teams):
    c, rows = one_claim("ellis-agrees-to-two-year-extension-with-blues", teams)
    # "$775,000 (NHL) and $175,000 (AHL) in the 2025-26 season, and $775,000 (NHL) and $225,000 (AHL) in 2026-27"
    assert c.total_usd is None and c.nhl_salary_usd == 775_000 and c.minors_salary_usd == 175_000
    assert {s: r.minors_salary_usd for s, r in rows.items()} == {"2025-26": 175_000, "2026-27": 225_000}
    assert (c.start_season, c.end_season) == ("2025-26", "2026-27")
    assert c.aav_usd == 775_000 and "derived:aav_from_season_salaries" in c.flags
    assert "season_sum_mismatch" not in c.flags


def test_wilsby_breakdown_states_the_span(teams):
    c, rows = one_claim("predators-sign-adam-wilsby-to-two-year-contract", teams)
    # signed Feb. 2025: "$775,000 at the NHL level in 2025-26 (two-way) and $775,000 in 2026-27 (one-way)"
    assert list(rows) == ["2025-26", "2026-27"]
    assert (c.start_season, c.end_season) == ("2025-26", "2026-27") and "span_from_season_breakdown" in c.flags
    assert c.aav_usd == 775_000


def test_no_aav_from_seasons_with_a_signing_bonus(teams):
    c, _ = one_claim("canes-sign-kochetkov", teams)
    assert "derived:aav_from_season_salaries" not in c.flags
