"""Historical CapFriendly team pages: the deadline collector and the 2019-2024 roster parser.

Fixtures are real Carolina captures, one per season:
2019-11-19 (2019-20), 2021-03-06 (2020-21), 2022-02-26 (2021-22), 2023-02-27 (2022-23)
on the 2019-2023 layout, and 2024-01-05 (2023-24) on the 2024 layout.
"""

import csv
import gzip
import json
from datetime import date

import pytest
from conftest import FIXTURES

from nhl_contracts.collectors import wayback_capfriendly as wc
from nhl_contracts.normalize import deadline_rosters as dr
from nhl_contracts.store import Store

WAYBACK = FIXTURES / "wayback"
CAR = {2019: "20191119082521", 2020: "20210306013630", 2021: "20220226231903", 2022: "20230227173941",
       2023: "20240105095249"}


def snap(ts, path="/teams/hurricanes"):
    return f"https://web.archive.org/web/{ts}/https://www.capfriendly.com{path}"


def html_of(season):
    return (WAYBACK / f"teams-hurricanes-{CAR[season]}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pages():
    return {s: dr.parse_deadline_page(html_of(s), snap(ts), ts) for s, ts in CAR.items()}


def player(rows, name):
    hits = [r for r in rows if r["player_name"] == name]
    assert len(hits) == 1, name
    return hits[0]


# -- parser ------------------------------------------------------------------

def test_layouts():
    assert [dr.layout_of(html_of(s)) for s in sorted(CAR)] == ["legacy"] * 4 + ["2024"]


@pytest.mark.parametrize("season,as_of,players,roster,groups", [
    (2019, "2019-11-19", 45, 22, {"Forwards", "Defense", "Goalies"}),
    (2020, "2021-03-06", 47, 27, {"Forwards", "Defense", "Goalies", "Taxi Squad"}),
    (2021, "2022-02-26", 53, 24, {"Forwards", "Defense", "Goalies", "LTIR"}),
    (2022, "2023-02-27", 47, 24, {"Forwards", "Defense", "Goalies", "LTIR"}),
    (2023, "2024-01-05", 41, 23, {"Forwards", "Defense", "Goalies", "IR"}),
])
def test_every_season_parses(pages, season, as_of, players, roster, groups):
    rows, team = pages[season]
    assert team["season"] == dr.season_label(season) and team["as_of"] == as_of and team["team"] == "CAR"
    assert team["players"] == len(rows) == players and team["roster_players"] == roster
    assert {r["group"] for r in rows} == groups
    assert {r["section"] for r in rows} == {"ROSTER", "NON-ROSTER"}
    assert all(r["season"] == team["season"] and r["tier"] == "E" for r in rows)
    assert all(r["capfriendly_path"].startswith("/players/") for r in rows)
    assert all(r["birth_date"] for r in rows)
    assert all(r["clause_status"] in ("clause", "none") for r in rows if r["cap_hit_usd"] is not None)
    assert team["projected_cap_hit_usd"] > 70_000_000


def test_contract_fields(pages):
    rows, _ = pages[2021]
    aho = player(rows, "Sebastian Aho")
    assert aho["cap_hit_usd"] == 8_460_250 and aho["years_remaining"] == 3 and aho["years_left"] == 2
    assert aho["end_season"] == "2023-24" and aho["expiry_status"] == "UFA"
    assert aho["clause"] is None and aho["clause_status"] == "none" and aho["birth_date"] == "1997-07-26"
    # a clause that starts in a later season: none this season, listed per season
    sv = player(rows, "Andrei Svechnikov")
    assert sv["clause_status"] == "none" and json.loads(sv["clause_seasons"])[0] == ["2025-26", "M-NTC"]
    staal = player(rows, "Jordan Staal")
    assert staal["clause"] == "NMC" and dr.clause_kind(staal["clause"]) == "full"
    assert staal["years_left"] == 1 and staal["expiry_status"] == "UFA"
    slavin = player(rows, "Jaccob Slavin")
    assert slavin["clause"] == "M-NTC" and dr.clause_kind(slavin["clause"]) == "modified"
    assert dr.clause_kind(None) is None and dr.clause_kind("NMC, M-NTC") == "full"


def test_ltir_rows_and_team_summary(pages):
    rows, team = pages[2021]
    g = player(rows, "Jake Gardiner")
    assert g["group"] == "LTIR" and g["ltir"] and g["status"] == "LTIR" and g["cap_hit_usd"] == 4_050_000
    assert g["clause"] == "M-NTC"
    assert team["projected_ltir_used_usd"] == 2_266_550 and team["projected_cap_space_usd"] == 0
    assert team["ltir_players"] == 1 and team["ltir_cap_hit_usd"] == 4_050_000
    assert sum(r["ltir"] for r in rows) == 1
    rows22, team22 = pages[2022]
    assert team22["ltir_players"] == 3 and team22["ltir_cap_hit_usd"] == 12_550_000
    assert team22["projected_ltir_used_usd"] == 3_157_777
    _, team19 = pages[2019]
    assert team19["projected_ltir_used_usd"] == 0 and team19["ltir_players"] == 0


def test_2019_clause_spelling_is_normalized(pages):
    rows, _ = pages[2019]
    g = player(rows, "Jake Gardiner")
    assert g["clause"] == "M-NTC" and g["clause_status"] == "clause"
    assert json.loads(g["clause_seasons"])[0] == ["2019-20", "M-NTC"]
    assert dr.normalize_clause("Modified NTC") == "M-NTC" and dr.normalize_clause("") is None


def test_2023_24_on_the_2024_layout(pages):
    rows, team = pages[2023]
    aho = player(rows, "Sebastian Aho")
    # the 8-year extension signed in July 2023 is on the page (years left counts it)
    assert aho["years_remaining"] == 9 and aho["end_season"] == "2031-32" and aho["clause_status"] == "none"
    assert json.loads(aho["clause_seasons"])[0] == ["2024-25", "NMC"]
    assert player(rows, "Teuvo Teräväinen")["clause"] == "M-NTC"
    assert sum(r["injured_reserve"] for r in rows) == 1
    assert team["projected_ltir_used_usd"] == 0


def test_non_roster_and_two_way(pages):
    rows, _ = pages[2021]
    minors = [r for r in rows if r["section"] == "NON-ROSTER"]
    assert minors and all(r["status"] in ("Minor", "Junior", "Loan") for r in minors)
    assert any(r["two_way"] for r in minors)


def test_team_codes():
    assert dr.team_code_for("coyotes", 2023) == "ARI" and dr.team_code_for("coyotes", 2024) == "UTA"
    assert dr.team_code_for("hurricanes", 2019) == "CAR" and dr.team_code_for("nope", 2020) is None


def test_assign_ids(pages):
    rows = [dict(r) for r in pages[2021][0]]
    n = dr.assign_ids(rows, {8478427: ("Sebastian Aho", "CAR"), 1: ("Jordan Staal", "CAR"),
                             2: ("Jordan Staal", "PIT")})
    assert n == 1 and player(rows, "Sebastian Aho")["player_id"] == 8478427
    assert player(rows, "Jordan Staal")["player_id"] is None  # two players share the name


def test_no_season_columns_raises():
    with pytest.raises(ValueError):
        dr.parse_deadline_page("<html><body>nothing</body></html>", snap("20220101000000"), "20220101000000")


# -- collector ---------------------------------------------------------------

def test_targets_and_plan():
    assert wc.deadline_targets(2021) == [date(2022, 1, 1), date(2022, 2, 1), date(2022, 3, 7), date(2022, 3, 21)]
    plan = wc.deadline_plan([2020, 2021], ["kraken", "hurricanes"])
    assert len(plan["https://www.capfriendly.com/teams/hurricanes"]) == 8
    assert len(plan["https://www.capfriendly.com/teams/kraken"]) == 4  # Seattle starts in 2021-22
    assert "utah" not in wc.HISTORICAL_SLUGS and len(wc.HISTORICAL_SLUGS) == 32
    key = wc.deadline_key("https://www.capfriendly.com/teams/hurricanes", date(2022, 3, 21))
    assert key.endswith("#before=2022-03-21") and wc.target_of(key) == date(2022, 3, 21)
    assert wc.season_of(date(2022, 3, 21)) == 2021 and wc.season_of(date(2021, 7, 1)) == 2021


def test_classify_deadline():
    html = html_of(2021)
    assert wc.page_season(html) == 2021
    assert wc.classify_deadline(html, "20220226231903", date(2022, 3, 7)) == "ok"
    assert wc.classify_deadline(html, "20220226231903", date(2022, 2, 26)) == "late"
    assert wc.classify_deadline(html, "20220226231903", date(2023, 1, 1)) == "stale"
    assert wc.classify_deadline("<html>Thank You</html>", "20220226231903", date(2022, 3, 7)) == "error"


class FakeArchive:
    """CDX answers and id_ snapshots for Carolina's 2021-22 season."""

    def __init__(self, redirect=None):
        self.calls = []
        self.redirect = redirect or {}  # requested ts -> served ts
        self.html = html_of(2021)

    def get_bytes(self, url, params=None, *, ok_statuses=(200,)):
        self.calls.append((url, params))
        if url == wc.CDX:
            assert params["url"] == "capfriendly.com/teams/hurricanes"
            assert params["from"] == "20210701" and params["to"] == "20220320"
            rows = [["timestamp", "original", "length"]] + [
                [ts, "https://www.capfriendly.com/teams/hurricanes", "21000"]
                for ts in ("20210723175447", "20211220024549", "20220216203510", "20220226231903",
                           "20220312202212", "20220325000000")]
            return 200, json.dumps(rows).encode(), url
        ts = url.split("/web/")[1][:14]
        served = self.redirect.get(ts, ts)
        final = f"https://web.archive.org/web/{served}id_/https://www.capfriendly.com/teams/hurricanes"
        if served == "20210723175447":
            return 200, b"<html>no roster table here, but more than twelve thousand bytes" + b" " * 13000, final
        return 200, self.html.encode(), final


def test_collect_deadline_picks_last_capture_before_each_target(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    fake = FakeArchive()
    out = wc.collect_deadline(store, fake, seasons=[2021], teams=["hurricanes"], pause=0)
    assert out["statuses"] == {"ok": 4} and out["fetched"] == 3
    got = {wc.target_of(p["url"]): p["snapshot_ts"] for p in store.wayback_pages(kind=wc.DEADLINE_KIND)}
    assert got == {date(2022, 1, 1): "20211220024549", date(2022, 2, 1): "20211220024549",
                   date(2022, 3, 7): "20220226231903", date(2022, 3, 21): "20220312202212"}
    # 1 CDX query + 3 distinct snapshots (Dec 20 reused for Jan 1 and Feb 1)
    assert len(fake.calls) == 4
    assert store.counts()["wayback_pages"] == 4 and not list(store.wayback_pages(kind="team"))

    again = FakeArchive()
    out2 = wc.collect_deadline(store, again, seasons=[2021], teams=["hurricanes"], pause=0)
    assert again.calls == [] and out2["fetched"] == 0  # resumable: nothing left to do


def test_collect_deadline_rejects_a_redirect_past_the_target(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    # Wayback serves Feb 26 for the Feb 16 request: too late for the Feb 1 target
    fake = FakeArchive(redirect={"20220216203510": "20220226231903"})
    wc.collect_deadline(store, fake, seasons=[2021], teams=["hurricanes"], pause=0)
    got = {wc.target_of(p["url"]): p["snapshot_ts"] for p in store.wayback_pages(kind=wc.DEADLINE_KIND)}
    assert got[date(2022, 3, 7)] == "20220226231903"
    assert got[date(2022, 2, 1)] == "20211220024549"


def test_collect_deadline_missing_and_max_pages(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    out = wc.collect_deadline(store, FakeArchive(), seasons=[2021], teams=["hurricanes"], pause=0, max_pages=1)
    assert out["fetched"] == 1 and "max_pages" in out["note"]

    class Empty:
        def get_bytes(self, url, params=None, *, ok_statuses=(200,)):
            return 200, json.dumps([["timestamp", "original", "length"]]).encode(), url

    store2 = Store(tmp_path / "raw2.sqlite")
    out2 = wc.collect_deadline(store2, Empty(), seasons=[2021], teams=["hurricanes"], pause=0)
    assert out2["statuses"] == {"missing": 4}
    assert store2.wayback_page_status(
        wc.deadline_key("https://www.capfriendly.com/teams/hurricanes", date(2022, 3, 21))) == "missing"


# -- store -> CSV ------------------------------------------------------------

def test_rows_from_store_and_write(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    url = "https://www.capfriendly.com/teams/hurricanes"
    for season, ts in CAR.items():
        target = wc.DEADLINES[season]
        store.upsert_wayback_page(wc.deadline_key(url, target), wc.DEADLINE_KIND, ts, snap(ts),
                                  gzip.compress(html_of(season).encode()), "ok", "2026-11-08T00:00:00+00:00")
    # a page filed under the wrong season's target is reported, not used
    store.upsert_wayback_page(wc.deadline_key(url, date(2019, 12, 1)), wc.DEADLINE_KIND, CAR[2021],
                              snap(CAR[2021]), gzip.compress(html_of(2021).encode()), "ok", "2026-11-08T00:00:00+00:00")
    store.set_players({8478427: ("Sebastian Aho", "CAR")}, "2026-11-08")
    players, teams, problems = dr.rows_from_store(store)
    assert len(teams) == 5 and len(players) == 45 + 47 + 53 + 47 + 41
    assert len(problems) == 1 and "page season 2021-22" in problems[0]
    assert {r["player_id"] for r in players if r["player_name"] == "Sebastian Aho"} == {8478427}
    assert all(r["target"] for r in players)

    counts = dr.write(players, teams, tmp_path / "out")
    assert counts == {"deadline_rosters": len(players), "deadline_teams": 5}
    with (tmp_path / "out" / "deadline_rosters.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == list(dr.ROSTER_COLUMNS)
    g = next(r for r in rows if r["player_name"] == "Jake Gardiner" and r["season"] == "2021-22")
    assert g["ltir"] == "1" and g["clause"] == "M-NTC" and g["cap_hit_usd"] == "4050000"
    with (tmp_path / "out" / "deadline_teams.csv").open() as f:
        t = [r for r in csv.DictReader(f) if r["season"] == "2021-22"]
    assert t[0]["projected_ltir_used_usd"] == "2266550" and t[0]["target"] == "2022-03-21"


def test_extension_signed_in_the_last_season():
    # older layout: "1 - 8 UFA" (Clayton Keller, Arizona, Dec 2019)
    r = {"name": "Clayton Keller", "birth_date": "1998-07-29", "player_path": "/players/clayton-keller",
         "position": "C", "status": "NHL", "years_remaining": 1, "expiry_status": "- 8 UFA",
         "seasons": [{"season": "2019-20", "cap_hit": 885_833, "aav": 885_833, "clause": None, "two_way": False},
                     {"season": "2020-21", "cap_hit": 7_150_000, "aav": 7_150_000, "clause": None,
                      "two_way": False}],
         "clause_detail": None, "flags": []}
    row = dr._roster_row(r, "Forwards", "ROSTER", 2019, {"season": "2019-20"})
    assert row["years_remaining"] == 9 and row["extension_years"] == 8 and row["years_left"] == 8
    assert row["end_season"] == "2027-28" and row["expiry_status"] == "UFA" and row["cap_hit_usd"] == 885_833
