"""PuckPedia Bluesky posts: parser formats, fixture coverage and the feed collector."""

import json
import re
from datetime import date

import pytest
from conftest import FIXTURES

from nhl_contracts.collectors import bluesky_puckpedia
from nhl_contracts.normalize import puckpedia as pp
from nhl_contracts.store import Store

POSTS = json.loads((FIXTURES / "bluesky" / "puckpedia-posts-2025-10-to-2026-10.json").read_text(encoding="utf-8"))


def fixture_post(snippet: str) -> dict:
    """The one fixture post whose text contains ``snippet``, in the store's row shape."""
    hits = [p for p in POSTS if snippet in p["text"]]
    assert hits, f"no fixture post contains {snippet!r}"
    p = hits[0]
    return {"uri": p["uri"], "created_at": p["at"], "text": p["text"], "links": []}


def parse(snippet: str):
    claims, seasons, clauses, source = pp.extract_post(fixture_post(snippet))
    assert len(claims) == 1 and source is not None, snippet
    return claims[0], seasons, clauses, source


def by_season(seasons):
    return {s.season: s for s in seasons}


# --- formats ---------------------------------------------------------------------

def test_extension_with_salary_and_signing_bonus_breakdown():
    c, seasons, clauses, src = parse("Marat Khusnutdinov to a three year")
    assert (c.player_name, c.team, c.contract_type, c.term_years) == ("Marat Khusnutdinov", "BOS", "extension", 3)
    assert c.cap_hit_usd == 3_950_000 and c.aav_usd is None
    assert (c.start_season, c.end_season) == ("2027-28", "2029-30")
    assert "start_season_assumed" in c.flags
    s = by_season(seasons)
    assert (s["2027-28"].base_salary_usd, s["2027-28"].signing_bonus_usd) == (2_200_000, 1_500_000)
    assert (s["2028-29"].base_salary_usd, s["2028-29"].signing_bonus_usd) == (3_800_000, 500_000)
    assert (s["2029-30"].base_salary_usd, s["2029-30"].signing_bonus_usd) == (3_850_000, None)
    assert s["2027-28"].total_salary_usd == 3_700_000 and s["2029-30"].cap_hit_usd == 3_950_000
    assert c.total_usd == 11_850_000 and "derived:total_from_seasons" in c.flags
    assert c.expiry_status == "UFA" and c.agent == "Paul Theofanous"
    assert "ref:https://puckpedia.com/signing/10842" in c.flags
    assert "https://puckpedia.com/signing/10842" in c.evidence
    rkey = fixture_post("Marat Khusnutdinov to a three year")["uri"].rsplit("/", 1)[1]
    assert src.url == f"https://bsky.app/profile/puckpedia.com/post/{rkey}" and c.source_url == src.url
    assert src.source_id == c.source_id == f"bsky:{rkey}"
    assert (src.kind, src.publisher, src.confidence_tier) == ("bluesky_puckpedia", pp.PUBLISHER, "D")
    assert (c.confidence_tier, c.extracted_by, c.signing_date_stated) == ("D", "regex", False)
    assert c.signed_at == "2026-10-22" and not clauses


def test_one_year_one_way_with_agent_handles_stripped():
    c, seasons, _, _ = parse("David Perron to a one year 1-way")
    assert (c.team, c.term_years, c.cap_hit_usd, c.one_way) == ("DAL", 1, 850_000, True)
    assert c.agent == "Allan Walsh" and c.start_season == "2026-27" and not seasons


def test_elc_cal_hit_typo_and_positional_bonus_columns():
    c, seasons, _, _ = parse("Kirill Zarubin to a 3 year ELC")
    assert (c.team, c.contract_type, c.term_years) == ("CGY", "ELC", 3)
    assert (c.cap_hit_usd, c.aav_usd) == (1_007_500, 1_075_000)
    s = by_season(seasons)
    y1, y3 = s["2026-27"], s["2028-29"]
    assert (y1.base_salary_usd, y1.signing_bonus_usd, y1.performance_bonus_usd, y1.minors_salary_usd) == (
        850_000, 102_500, 72_500, 85_000)
    assert (y3.base_salary_usd, y3.signing_bonus_usd, y3.performance_bonus_usd, y3.minors_salary_usd) == (
        950_000, 112_500, 62_500, 85_000)
    assert c.one_way is False and c.nhl_salary_usd == 850_000 and c.minors_salary_usd == 85_000


def test_elc_three_values_mean_no_bonus_and_comma_aav():
    c, seasons, _, _ = parse("Max Psenicka to a three year ELC")
    assert (c.cap_hit_usd, c.aav_usd) == (997_500, 1_049_167)
    y3 = by_season(seasons)["2028-29"]
    assert (y3.base_salary_usd, y3.signing_bonus_usd, y3.performance_bonus_usd, y3.minors_salary_usd) == (
        950_000, 97_500, None, 85_000)


def test_two_way_nhl_minors_guaranteed():
    c, seasons, _, _ = parse("Reilly Walsh to a one year two-way")
    assert (c.team, c.term_years, c.one_way) == ("MTL", 1, False)
    assert (c.nhl_salary_usd, c.minors_salary_usd, c.cap_hit_usd) == (850_000, 375_000, None)
    assert "guaranteed:450000" in c.flags
    assert len(seasons) == 1 and seasons[0].minors_salary_usd == 375_000 and seasons[0].one_way is False


def test_bare_cap_hit_is_millions_and_clause_ranges():
    c, seasons, clauses, _ = parse("Brett Kulak to a 5 year $4.5 cap hit")
    assert c.cap_hit_usd == 4_500_000 and "unit_assumed" in c.flags
    s = by_season(seasons)
    assert len(s) == 5 and s["2029-30"].base_salary_usd == s["2030-31"].base_salary_usd == 3_600_000
    assert c.total_usd == 22_500_000
    got = {(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses}
    assert got == {("NTC", None, "2026-27", "2028-29"), ("M-NTC", 15, "2029-30", "2030-31")}
    assert s["2030-31"].clause == "15 team M-NTC"


def test_re_signed_with_per_season_one_way():
    c, seasons, _, _ = parse("Carl Lindbom to 3 year $900K")
    assert (c.team, c.re_signed, c.term_years, c.cap_hit_usd) == ("VGK", True, 3, 900_000)
    s = by_season(seasons)
    assert (s["2026-27"].base_salary_usd, s["2026-27"].minors_salary_usd, s["2026-27"].one_way) == (
        850_000, 300_000, False)
    assert (s["2027-28"].base_salary_usd, s["2027-28"].one_way) == (900_000, True)
    assert (s["2028-29"].base_salary_usd, s["2028-29"].one_way) == (950_000, True)
    assert c.one_way is None and "one_way_varies_by_season" in c.flags


def test_slash_shorthand_and_ntc_in_yr_5_and_6():
    c, seasons, clauses, _ = parse("Adam Fantilli to a six year")
    s = by_season(seasons)
    assert (s["2027-28"].base_salary_usd, s["2027-28"].signing_bonus_usd) == (5_000_000, 10_000_000)
    assert s["2030-31"].base_salary_usd == 12_500_000 and s["2031-32"].base_salary_usd == 12_000_000
    assert [(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses] == [
        ("M-NTC", 15, "2030-31", "2031-32")]


def test_accented_name_ranges_and_m_ntc():
    c, seasons, clauses, _ = parse("Noah Östlund to an eight year")
    assert c.player_name == "Noah Östlund" and c.contract_key.startswith("noah ostlund|BUF|")
    s = by_season(seasons)
    assert len(s) == 8 and all(s[x].base_salary_usd == 6_000_000 for x in ("2031-32", "2032-33", "2033-34"))
    assert (s["2027-28"].base_salary_usd, s["2027-28"].signing_bonus_usd) == (3_850_000, 3_750_000)
    assert [(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses] == [
        ("M-NTC", 7, "2030-31", "2033-34")]


def test_full_nmc_both_years():
    c, seasons, clauses, _ = parse("Mark Stone to a two year")
    assert c.total_usd == 18_000_000
    assert [(k.kind, k.season_from, k.season_to) for k in clauses] == [("NMC", "2026-27", "2027-28")]
    assert all(s.clause == "NMC" for s in seasons)


def test_stated_start_and_flat_salary():
    c, seasons, clauses, _ = parse("Tim Washe to a four year")
    assert (c.team, c.start_season, c.end_season) == ("ANA", "2027-28", "2030-31")
    assert "start_season_assumed" not in c.flags
    assert [s.base_salary_usd for s in seasons] == [2_250_000] * 4 and c.total_usd == 9_000_000
    assert [(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses] == [
        ("M-NTC", 10, "2029-30", "2030-31")]


def test_no_trade_protection_and_all_salary():
    c, seasons, clauses, _ = parse("Paul Cotter to a 1 year")
    assert (c.team, c.cap_hit_usd, c.aav_usd) == ("VAN", 2_150_000, None)
    assert "no_trade_protection" in c.flags and not clauses
    assert seasons[0].base_salary_usd == 2_150_000 and "derived:all_salary_from_cap_hit" in c.flags


def test_draft_pick_prefix_and_unit_less_slashes():
    c, seasons, _, _ = parse("Vladimír Dravecký to a three year ELC")
    assert (c.player_name, c.team, c.contract_type) == ("Vladimír Dravecký", "NYI", "ELC")
    assert (c.cap_hit_usd, c.aav_usd) == (1_008_000, 1_075_000)
    y2 = by_season(seasons)["2027-28"]
    assert (y2.base_salary_usd, y2.signing_bonus_usd, y2.performance_bonus_usd, y2.minors_salary_usd) == (
        900_000, 107_500, 67_500, 87_500)


def test_recently_acquired_cap_hit_elc():
    c, seasons, _, _ = parse("Sutter Muzzatti to a two year")
    assert (c.player_name, c.team, c.contract_type, c.term_years, c.cap_hit_usd) == (
        "Sutter Muzzatti", "DET", "ELC", 2, 1_050_000)
    assert len(seasons) == 2


def test_have_signed():
    c, _, _, _ = parse("Vitek Vanecek to a 1 year")
    assert (c.player_name, c.team, c.term_years, c.cap_hit_usd) == ("Vitek Vanecek", "NYI", 1, 1_000_000)


def test_bare_all_salary_is_millions_and_rfa_with_arbitration():
    c, seasons, _, _ = parse("Bobby Brink to a 1 year")
    assert (c.team, c.cap_hit_usd, c.expiry_status) == ("MIN", 2_750_000, "RFA")
    assert "rfa_arbitration_rights" in c.flags and seasons[0].base_salary_usd == 2_750_000


def test_range_without_colon():
    c, seasons, clauses, _ = parse("Sam Steel to a four year")
    assert [s.base_salary_usd for s in seasons] == [4_000_000, 4_000_000, 3_500_000, 3_300_000]
    assert [(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses] == [
        ("M-NTC", 6, "2027-28", "2028-29")]


def test_glued_year_lines_and_surname_from_link():
    c, seasons, clauses, _ = parse("F McMichael to 6yr")
    assert c.player_name == "Connor McMichael" and "first_name_from_link" in c.flags
    assert [s.base_salary_usd for s in seasons] == [7_500_000] + [6_750_000] * 4 + [6_000_000]
    assert [(k.kind, k.list_size, k.season_from, k.season_to) for k in clauses] == [
        ("M-NTC", 3, "2028-29", "2031-32")]


def test_name_first_post_without_term_derives_it_from_the_breakdown():
    c, seasons, clauses, _ = parse("The Hischier extension")
    assert (c.player_name, c.team, c.term_years) == ("Nico Hischier", "NJD", 5)
    assert "derived:term_from_seasons" in c.flags and c.cap_hit_usd is None
    assert {(k.kind, k.list_size) for k in clauses} == {("NMC", None), ("M-NTC", 18), ("M-NTC", 14)}


def test_aav_only_header():
    c, _, _, _ = parse("$11M AAV contract extension with 34 y/o F Artemi Panarin")
    assert (c.player_name, c.aav_usd, c.cap_hit_usd, c.contract_type) == ("Artemi Panarin", 11_000_000, None,
                                                                          "extension")


def test_minors_typo_is_flagged_and_kept_as_written():
    c, seasons, _, _ = parse("Owen Protz to a 3 year ELC")
    assert "suspicious_amount" in c.flags and c.total_usd is None
    assert by_season(seasons)["2026-27"].minors_salary_usd == 85_000_000


def test_cap_hit_breakdown_mismatch_is_flagged():
    c, _, _, _ = parse("Kirill Marchenko 6yr")
    assert c.total_usd == 73_167_000 and "total_vs_cap_hit_mismatch" in c.flags


@pytest.mark.parametrize("snippet, kind", [
    ("To #ALLCAPS\nJordan Kyrou", "trade"),
    ("added Max Domi to LTIR", "ltir"),
    ("PuckDrop Newsletter is out", "other"),
    ("opening roster has $4.4M", "other"),
    ("The first PTOs of the offseason", "other"),
])
def test_non_signings_yield_nothing(snippet, kind):
    post = fixture_post(snippet)
    assert pp.classify_post(post["text"]) == kind
    assert pp.extract_post(post) == ([], [], [], None)


def test_waivers_post():
    text = "Waivers today:\n\nGarrett Wilson #LetsGoFlyers\nZach Dean #stlblues"
    assert pp.classify_post(text) == "waivers"


def test_links_from_facets_win_over_truncated_text():
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "created_at": "2026-07-01T16:00:00Z",
            "text": "The #NYR signed 24 y/o D Braden Schneider to a one year $5.5M cap hit contract\n\n"
                    "puckpedia.com/player/braden-sch...",
            "links": ["https://puckpedia.com/player/braden-schneider?6a55444b213bb"]}
    (c,), _, _, src = pp.extract_post(post)
    assert "ref:https://puckpedia.com/player/braden-schneider" in c.flags
    assert src.source_id == "bsky:abc" and src.url == "https://bsky.app/profile/puckpedia.com/post/abc"


def test_surname_expanded_from_player_link_even_when_a_signing_link_is_preferred():
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/def", "created_at": "2026-07-16T16:00:00Z",
            "text": "The #stlblues signed 25 y/o RFA F McMichael to 6yr $6.75M cap hit deal",
            "links": ["https://puckpedia.com/signing/10800", "https://puckpedia.com/player/connor-mcmichael"]}
    (c,), _, _, _ = pp.extract_post(post)
    assert c.player_name == "Connor McMichael" and "ref:https://puckpedia.com/signing/10800" in c.flags


# --- fixture coverage -----------------------------------------------------------------

def test_every_signing_hashtag_maps_to_a_team():
    for p in POSTS:
        if pp.classify_post(p["text"]) != "signing":
            continue
        head = pp.normalize_text(p["text"]).split("\n")[0]
        _, unmapped = pp.team_tags(head)
        assert not unmapped, (unmapped, head)


def test_fixture_coverage():
    signings = [p for p in POSTS if pp.classify_post(p["text"]) == "signing"]
    assert len(signings) > 400
    claims, seasons, clauses, sources = pp.normalize_posts(
        {"uri": p["uri"], "created_at": p["at"], "text": p["text"], "links": []} for p in POSTS)
    assert len(claims) == len(sources) == len(signings)
    good = [c for c in claims if c.team and c.term_years and (c.cap_hit_usd or c.aav_usd or c.nhl_salary_usd)]
    assert len(good) / len(signings) >= 0.95
    assert len({c.claim_id for c in claims}) == len(claims)

    rows: dict[str, list] = {}
    for s in seasons:
        rows.setdefault(s.claim_id, []).append(s)
    with_breakdown = [c for c, p in zip(claims, signings, strict=True) if re.search(r"\bYe?a?r 1\b", p["text"])]
    assert len(with_breakdown) > 200
    incomplete = [c for c in with_breakdown if "season_breakdown_incomplete" in c.flags]
    assert len(incomplete) <= 2
    for c in with_breakdown:
        if c not in incomplete:
            assert len(rows.get(c.claim_id, [])) == c.term_years, c.evidence
    assert all(s.confidence_tier == "D" for s in seasons) and all(k.kind in ("NMC", "NTC", "M-NTC") for k in clauses)


# --- collector --------------------------------------------------------------------------

def feed_item(rkey: str, created: str, text: str, *, handle: str = "puckpedia.com", reason: bool = False) -> dict:
    item = {"post": {
        "uri": f"at://did:plc:pp/app.bsky.feed.post/{rkey}", "cid": f"cid-{rkey}",
        "author": {"handle": handle},
        "record": {"createdAt": created, "text": text,
                   "facets": [{"features": [{"$type": "app.bsky.richtext.facet#link",
                                             "uri": f"https://puckpedia.com/signing/{rkey}"}]}],
                   "embed": {"$type": "app.bsky.embed.external",
                             "external": {"uri": f"https://puckpedia.com/signing/{rkey}"}}},
    }}
    if reason:
        item["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost"}
    return item


class FakeFeed:
    """Same idea as FakeClient in test_collectors_and_export: canned pages keyed by cursor."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        assert url == bluesky_puckpedia.FEED_URL and params["actor"] == "puckpedia.com"
        return self.pages[params.get("cursor")]


PAGES = {
    None: {"cursor": "p2", "feed": [
        feed_item("a3", "2026-10-22T23:00:00Z", "The #NYR signed 37 y/o F Luke Glendening to a one year $850K cap hit"),
        feed_item("rp", "2026-10-22T22:00:00Z", "a repost", handle="someone.else", reason=True),
        feed_item("ot", "2026-10-22T21:00:00Z", "another account", handle="someone.else"),
        feed_item("a2", "2026-10-21T23:00:00Z", "🚨 Trade\n\nTo #NYR\n..."),
    ]},
    "p2": {"feed": [feed_item("a1", "2025-06-30T23:00:00Z", "older post")]},
}


def test_bluesky_collect_is_idempotent_and_skips_reposts(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    client = FakeFeed(PAGES)
    first = bluesky_puckpedia.collect(store, client, pause=0)
    assert first == {"requests": 2, "new": 3, "pages": 2}
    rows = list(store.bsky_posts("puckpedia.com"))
    assert [r["uri"].rsplit("/", 1)[1] for r in rows] == ["a1", "a2", "a3"]
    assert rows[-1]["links"] == ["https://puckpedia.com/signing/a3"]
    assert json.loads(rows[-1]["raw"])["text"].startswith("The #NYR signed")
    assert store.counts()["bsky_posts"] == 3

    second = bluesky_puckpedia.collect(store, client, pause=0)
    assert second == {"requests": 1, "new": 0, "pages": 1}  # incremental: first page adds nothing -> stop
    assert store.counts()["bsky_posts"] == 3
    runs = store.conn.execute("SELECT collector, requests, new_rows, note FROM runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("bluesky_puckpedia", 2, 3, "pages=2 stop=end_of_feed"),
                                        ("bluesky_puckpedia", 1, 0, "pages=1 stop=no_new_rows")]


def test_bluesky_collect_since_and_max_pages(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    out = bluesky_puckpedia.collect(store, FakeFeed(PAGES), since=date(2026, 1, 1), pause=0)
    assert out["new"] == 2 and out["pages"] == 2  # the 2025 post is past the floor and not stored
    assert store.oldest_bsky_post("puckpedia.com").startswith("2026-10-21")

    store2 = Store(tmp_path / "raw2.sqlite")
    out = bluesky_puckpedia.collect(store2, FakeFeed(PAGES), max_pages=1, pause=0)
    assert out == {"requests": 1, "new": 2, "pages": 1}


def test_stored_posts_feed_the_parser(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    bluesky_puckpedia.collect(store, FakeFeed(PAGES), pause=0)
    claims, seasons, clauses, sources = pp.normalize_posts(store.bsky_posts("puckpedia.com"))
    assert [(c.player_name, c.team, c.cap_hit_usd) for c in claims] == [("Luke Glendening", "NYR", 850_000)]
    assert "ref:https://puckpedia.com/signing/a3" in claims[0].flags and len(sources) == 1


def test_header_term_contradicted_by_its_own_schedule():
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/3lt3wrlzf6n2s", "created_at": "2025-07-04T00:00:38.107Z",
            "links": [], "text": (
                "The #ALLCAPS signed 28 y/o UFA F Anthony Beauvillier to a 1 year $2.75M contract\n\n"
                "Yr 1: $850K Salary & $2.275M SB\nYr 2: $850K Salary & $1.525M SB\n\n"
                "Rep'd by Philippe Lecavalier @QuartexxHockey")}
    claims, seasons, _, _ = pp.extract_post(post)
    (c,) = claims
    assert c.term_years == 2 and "term_vs_breakdown_mismatch:1" in c.flags and "derived:term_from_seasons" in c.flags
    assert sorted(by_season(seasons)) == ["2025-26", "2026-27"]
