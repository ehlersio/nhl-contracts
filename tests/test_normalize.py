"""Story-level expectations against fixtures saved from real Forge responses."""

from conftest import load_story

from nhl_contracts.normalize.contracts import extract_espn, extract_story


def one(slug, teams):
    claims, source = extract_story(load_story(slug), teams)
    assert len(claims) == 1, [c.player_name for c in claims]
    return claims[0], source


def test_nhl_staff_story_total_and_aav(teams):
    c, src = one("marat-khusnutdinov-signs-three-year-contract-with-boston-bruins", teams)
    assert (c.player_name, c.player_id, c.team) == ("Marat Khusnutdinov", 8482177, "BOS")
    assert (c.term_years, c.total_usd, c.aav_usd) == (3, 11_850_000, 3_950_000)
    assert c.start_season == "2027-28" and c.end_season == "2029-30"
    assert c.confidence_tier == "B" and src.publisher == "NHL.com"
    assert "total_vs_aav_mismatch" not in c.flags


def test_club_release_cap_hit_and_extension(teams):
    c, src = one("bruins-sign-marat-khusnutdinov-to-three-year-contract-extension", teams)
    assert c.contract_type == "extension"
    assert c.term_years == 3 and c.cap_hit_usd == 3_950_000 and c.aav_usd is None
    assert c.end_season == "2029-30" and c.start_season == "2027-28"
    assert c.confidence_tier == "A" and src.author == "Eric Russo"


def test_broberg_total_aav_next_season(teams):
    c, _ = one("philip-broberg-signs-six-year-contract-with-st-louis-blues", teams)
    assert (c.term_years, c.total_usd, c.aav_usd, c.team) == (6, 48_000_000, 8_000_000, "STL")
    assert c.start_season == "2026-27" and c.end_season == "2031-32"


def test_club_typo_is_flagged_not_fixed(teams):
    # New Jersey wrote "$58,500,00" (missing a zero); we keep what they wrote and flag it.
    c, _ = one("hischier-agrees-to-terms-on-contract-extension-release", teams)
    assert c.player_name == "Nico Hischier" and c.term_years == 5 and c.aav_usd == 11_700_000
    assert c.total_usd == 5_850_000
    assert "total_vs_aav_mismatch" in c.flags and "per_season_breakdown" in c.flags


def test_below_minimum_typo_flag(teams):
    c, _ = one("canes-sign-joseph-to-one-year-contract", teams)
    assert c.player_name == "Pierre-Olivier Joseph" and c.aav_usd == 85_000
    assert "below_league_minimum" in c.flags


def test_two_way_salaries_and_one_year_derivation(teams):
    c, _ = one("canes-sign-reilly-to-one-year-two-way-contract", teams)
    assert c.one_way is False and c.nhl_salary_usd == 850_000 and c.minors_salary_usd == 450_000
    assert c.aav_usd == 850_000 and "derived:aav_from_nhl_salary" in c.flags


def test_aav_at_nhl_level_is_aav_not_salary(teams):
    c, _ = one("capitals-sign-oliver-suvanto", teams)
    assert c.contract_type == "ELC" and c.term_years == 3
    assert c.aav_usd == 1_075_000 and c.minors_salary_usd == 87_500


def test_multi_player_list_with_parentheticals(teams):
    claims, _ = extract_story(load_story("minnesota-wild-re-signs-forward-caedan-bankier"), teams)
    names = {c.player_name: c for c in claims}
    assert set(names) == {"Caedan Bankier", "Carson Lambos", "David Spacek"}
    assert names["Carson Lambos"].nhl_salary_usd == 850_000
    assert names["Carson Lambos"].minors_salary_usd == 110_000
    assert all(c.term_years == 1 and c.one_way is False for c in claims)
    assert all("multi_player_story" in c.flags for c in claims)


def test_multi_player_shared_aav(teams):
    claims, _ = extract_story(load_story("seattle-kraken-sign-forwards-lleyton-roed-and-j-r-avon"), teams)
    assert {c.player_name for c in claims} == {"Lleyton Roed", "J.R. Avon"}
    assert all(c.term_years == 1 and c.one_way is False for c in claims)


def test_two_signings_in_one_paragraph_do_not_bleed(teams):
    claims, _ = extract_story(load_story("dallas-stars-sign-forwards-arttu-hyry-and-artem-shlaine"), teams)
    by = {c.player_name: c for c in claims}
    assert by["Arttu Hyry"].term_years == 2 and by["Arttu Hyry"].end_season == "2027-28"
    assert by["Artem Shlaine"].term_years == 1 and by["Artem Shlaine"].end_season == "2026-27"
    assert "season_span_mismatch" not in by["Artem Shlaine"].flags


def test_calgary_labelled_block(teams):
    c, _ = one("flames-sign-defenceman-simon-nemec", teams)
    assert c.term_years == 5 and c.aav_usd == 7_250_000


def test_escaped_hyphen_and_all_caps_headline(teams):
    c, _ = one("canucks-agree-to-terms-with-defenceman-zeev-buium", teams)
    assert c.player_name == "Zeev Buium" and c.term_years == 8 and c.aav_usd == 9_380_000
    assert c.contract_type == "extension"


def test_name_before_verb_with_relative_clause(teams):
    c, _ = one("maple-leafs-acquire-kirill-marchenko-in-trade", teams)
    assert c.player_name == "Kirill Marchenko" and c.team == "TOR"
    assert (c.term_years, c.total_usd, c.aav_usd) == (6, 75_000_000, 12_500_000)
    assert "attributed_report" in c.flags


def test_surname_only_is_expanded(teams):
    c, _ = one("jacob-melanson-signs-three-year-deal", teams)
    assert c.player_name == "Jacob Melanson" and c.term_years == 3 and c.aav_usd == 1_450_000


def test_montreal_terms_on_contract_with(teams):
    c, _ = one("four-year-contract-for-zachary-bolduc", teams)
    assert c.player_name == "Zachary Bolduc" and c.term_years == 4 and c.aav_usd == 4_200_000
    assert (c.start_season, c.end_season) == ("2026-27", "2029-30")


def test_previous_contract_is_ignored(teams):
    c, _ = one("anaheim-ducks-sign-ryan-poehling-to-four-year-contract", teams)
    assert c.term_years == 4 and c.total_usd is None and c.aav_usd is None


def test_non_player_stories_yield_nothing(teams):
    for slug in (
        "minnesota-wild-signs-head-coach-john-hynes",
        "ottawa-senators-sign-travis-green-to-multi-year",
        "travis-green-signs-deal-as-ottawa-coach",
        "release-blackhawks-and-wgn-radio",
        "tampa-bay-lightning-syracuse-crunch-announce",
        "jets-sign-cole-kehler-as-their-emergency-backup",
        "release-condors-sign-eemil-vinni-to-one-year-ahl",
        "dallas-stars-hope-to-begin-contract-talks",
        "philadelphia-flyers-and-nexgen-hyperbaric",
    ):
        claims, _ = extract_story(load_story(slug), teams)
        assert claims == [], slug


def test_pto_type(teams):
    c, _ = one("tampa-bay-lightning-sign-forward-kole-lind-to-pto", teams)
    assert c.contract_type == "PTO" and c.player_id == 8479986


def test_espn_rows(espn_payload):
    rows = {t["description"]: t for t in espn_payload["transactions"]}
    def claims_for(desc):
        t = rows[desc]
        row = {"key": "k", "date": t["date"], "team_abbrev": t["team"]["abbreviation"], "description": desc}
        return extract_espn(row)[0]

    elc = claims_for("Signed D Juho Piiparinen to a three-year, entry-level contract.")
    assert elc[0].player_name == "Juho Piiparinen" and elc[0].contract_type == "ELC" and elc[0].term_years == 3
    assert elc[0].confidence_tier == "C" and elc[0].team == "VGK"
    multi = claims_for("Signed F Caedan Bankier and Ds Carson Lambos and David Spacek to one-year, two-way contracts.")
    assert [c.player_name for c in multi] == ["Caedan Bankier", "Carson Lambos", "David Spacek"]
    assert all(c.one_way is False and c.term_years == 1 for c in multi)
    nj = claims_for("Re-signed F Ben Steeves to a one-year, two-way contract. "
                    "Re-signed G Jakub Málek to a two-year contract.")
    assert [(c.player_name, c.term_years, c.re_signed, c.team) for c in nj] == [
        ("Ben Steeves", 1, True, "NJD"), ("Jakub Málek", 2, True, "NJD")]
