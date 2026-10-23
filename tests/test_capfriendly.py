import copy
import gzip
import json
import os

import pytest
from conftest import FIXTURES

from nhl_contracts.collectors import wayback_capfriendly as wc
from nhl_contracts.normalize import capfriendly as cf
from nhl_contracts.store import Store

WAYBACK = FIXTURES / "wayback"
TEAM_TS = "20240704221225"
MCDAVID_TS = "20240705184905"
BOURGAULT_TS = "20240609210049"


def snap(ts, path):
    return f"https://web.archive.org/web/{ts}/https://www.capfriendly.com{path}"


def read(name):
    return (WAYBACK / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def team_rows():
    return cf.parse_team_page(read(f"teams-oilers-{TEAM_TS}.html"), snap(TEAM_TS, "/teams/oilers"), TEAM_TS)


@pytest.fixture(scope="module")
def mcdavid():
    return cf.parse_player_page(read(f"players-connor-mcdavid-{MCDAVID_TS}.html"),
                                snap(MCDAVID_TS, "/players/connor-mcdavid"), MCDAVID_TS)


@pytest.fixture(scope="module")
def bourgault():
    return cf.parse_player_page(read(f"players-xavier-bourgault-{BOURGAULT_TS}.html"),
                                snap(BOURGAULT_TS, "/players/xavier-bourgault"), BOURGAULT_TS)


def row(rows, name):
    matches = [r for r in rows if r["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


# -- helpers -----------------------------------------------------------------

def test_value_helpers():
    assert cf.money("$12,500,000") == 12_500_000
    assert cf.money("$0") == 0
    assert cf.money("-$2,454,167") == -2_454_167
    assert cf.money("-") is None and cf.money("") is None and cf.money("UFA") is None
    assert cf.parse_date("Jul. 5, 2017") == "2017-07-05"
    assert cf.parse_date("Sept. 1, 2020") == "2020-09-01"
    assert cf.parse_date("June 30, 2024") == "2024-06-30"
    assert cf.flip_name("Nugent-Hopkins, Ryan") == "Ryan Nugent-Hopkins"
    assert cf.ts_to_iso("20240705184905") == "2024-07-05T18:49:05+00:00"
    assert len(cf.TEAM_SLUGS) == 33 and len(set(cf.TEAM_SLUGS.values())) == 32
    assert cf.TEAM_SLUGS["coyotes"] == cf.TEAM_SLUGS["utah"] == "UTA" and cf.TEAM_SLUGS["bruins"] == "BOS"
    assert cf.team_code("Arizona Coyotes") == ("UTA", True)


# -- team page ---------------------------------------------------------------

def test_team_page_roster_rows(team_rows):
    assert len(team_rows) == 47  # 22 NHL roster + 25 non-roster, TOTAL rows excluded
    assert not any(r["name"].upper().startswith("TOTAL") for r in team_rows)
    assert {r["status"] for r in team_rows} == {"NHL", "Minor", "Junior"}
    assert {r["section"] for r in team_rows} == {"ROSTER", "NON-ROSTER"}

    mc = row(team_rows, "Connor McDavid")
    assert mc["captaincy"] == "C" and mc["name_raw"] == "McDavid, Connor"
    assert mc["player_path"] == "/players/connor-mcdavid" and mc["team"] == "EDM"
    assert [s["season"] for s in mc["seasons"]] == ["2024-25", "2025-26"] and mc["expiry_season"] == "2026-27"
    assert mc["birth_date"] == "1997-01-13" and mc["acquired_detail"] == "2015 Round 1, #1 Overall"

    d = row(team_rows, "Leon Draisaitl")
    assert d["years_remaining"] == 1 and d["expiry_status"] == "UFA" and d["terms"] == "M-NTC, NMC"
    assert d["position"] == "C, LW" and d["status"] == "NHL" and d["age"] == 28 and d["captaincy"] == "A"
    assert "10 team trade list" in d["clause_detail"]
    s = d["seasons"][0]
    assert s == {"season": "2024-25", "cap_hit": 8_500_000, "aav": 8_500_000, "total_salary": 8_000_000,
                 "base_salary": 8_000_000, "performance_bonus": 0, "signing_bonus": 0,
                 "clause": "M-NTC, NMC", "two_way": False}
    assert len(d["seasons"]) == 1 and d["expiry_season"] == "2025-26"

    b = row(team_rows, "Xavier Bourgault")
    assert b["terms"] == "ELC" and b["status"] == "Minor" and b["section"] == "NON-ROSTER"
    assert b["years_remaining"] == 1 and b["expiry_status"] == "RFA"
    bs = b["seasons"][0]
    assert bs["two_way"] and bs["cap_hit"] == 925_000 and bs["aav"] == 1_167_500
    assert bs["total_salary"] == 80_000 and bs["performance_bonus"] == 300_000 and bs["signing_bonus"] == 92_500
    assert any("Maximum Potential Performance Bonuses: $300,000" in n for n in b["term_notes"])

    p = row(team_rows, "Calvin Pickard")
    assert p["seasons"][0]["base_salary"] == 850_000 and p["seasons"][0]["signing_bonus"] == 150_000
    assert p["seasons"][1]["base_salary"] == 1_000_000 and p["flags"] == ["unconfirmed"]

    assert row(team_rows, "Dylan Holloway")["seasons"] == []  # pending RFA: only the "RFA" marker cell
    assert row(team_rows, "Mattias Ekholm")["terms"] == "RS"
    assert row(team_rows, "Corey Perry")["terms"] == "35+"
    assert row(team_rows, "Beau Akey")["status"] == "Junior"


def test_team_page_buyouts():
    buyouts = cf.parse_team_buyouts(read(f"teams-oilers-{TEAM_TS}.html"), snap(TEAM_TS, "/teams/oilers"), TEAM_TS)
    assert [b["name"] for b in buyouts] == ["James Neal", "Jack Campbell"]
    neal, campbell = buyouts
    assert neal["seasons"] == [{"season": "2024-25", "cap_hit": 1_916_667, "aav": 0, "total_salary": 1_916_667,
                                "signing_bonus": 0}]
    assert [s["season"] for s in campbell["seasons"]] == ["2024-25", "2025-26", "2026-27", "2027-28", "2028-29",
                                                           "2029-30"]
    assert [s["cap_hit"] for s in campbell["seasons"]][:3] == [1_100_000, 2_300_000, 2_600_000]
    assert {s["total_salary"] for s in campbell["seasons"]} == {1_500_000}


def test_team_page_player_paths():
    paths = cf.team_page_player_paths(read(f"teams-oilers-{TEAM_TS}.html"))
    assert len(paths) == 49  # 47 roster/non-roster rows + 2 buyouts
    assert "/players/connor-mcdavid" in paths and "/players/jack-campbell" in paths
    assert all(p.startswith("/players/") for p in paths)


# -- player pages ------------------------------------------------------------

def test_player_page_mcdavid(mcdavid):
    assert mcdavid["name"] == "Connor McDavid" and mcdavid["number"] == 97 and mcdavid["current_team"] == "EDM"
    assert mcdavid["draft"] == {"year": 2015, "round": 1, "overall": 1, "team": "EDM"}
    assert mcdavid["birth_date"] == "1997-01-13" and mcdavid["bio"]["HEIGHT"] == "6'1\" 185cm"
    assert [c["section"] for c in mcdavid["contracts"]] == ["CURRENT CONTRACT", "CONTRACT HISTORY"]

    ext, elc = mcdavid["contracts"]
    assert ext["title"] == "Standard Contract (Extension)" and ext["contract_type"] == "extension"
    assert ext["signed_by"] == "Peter Chiarelli" and ext["length_years"] == 8 and ext["value_usd"] == 100_000_000
    assert ext["signing_date"] == "2017-07-05" and ext["signing_team"] == "EDM" and ext["expiry_status"] == "UFA"
    assert ext["cap_pct"] == "16.67" and ext["clause_source"] == "CapFriendly"
    assert ext["source"] == "@CapFriendly" and ext["source_url"].endswith("/883501317599186945")
    assert ext["summary"] == ("Connor McDavid signed a 8 year, $100,000,000 contract with the Edmonton Oilers on "
                              "Jul. 5, 2017. The contract has a cap hit of $12,500,000.")
    seasons = {s["season"]: s for s in ext["seasons"]}
    assert list(seasons) == [f"{y}-{str(y + 1)[2:]}" for y in range(2018, 2026)]
    assert seasons["2018-19"]["cap_hit"] == 12_500_000
    assert seasons["2018-19"]["base_salary"] == 2_000_000 and seasons["2018-19"]["signing_bonus"] == 13_000_000
    assert seasons["2018-19"]["total_salary"] == 15_000_000 and seasons["2018-19"]["minors_salary"] == 15_000_000
    assert seasons["2021-22"]["clause"] is None and seasons["2022-23"]["clause"] == "NMC"
    assert ext["totals"]["signing_bonus"] == 86_000_000  # TOTAL row kept apart, not a season

    assert elc["title"] == "Entry-Level Contract" and elc["contract_type"] == "ELC"
    assert elc["length_years"] == 3 and elc["value_usd"] == 11_325_000 and elc["signing_date"] == "2015-07-03"
    assert elc["seasons"][0]["cap_hit"] == 925_000 and elc["seasons"][0]["aav"] == 3_775_000
    assert elc["seasons"][0]["minors_salary"] == 70_000 and elc["notes"] == "Qualifying Offer: $874,125"


def test_player_page_two_way_elc(bourgault):
    (c,) = bourgault["contracts"]
    assert c["signed_by"] == "Ken Holland" and c["signing_date"] == "2022-03-31" and c["value_usd"] == 3_502_500
    s = c["seasons"][-1]
    # the team page shows $80,000 for this season: that is the minor-league salary
    assert s["season"] == "2024-25" and s["base_salary"] == 832_500 and s["minors_salary"] == 80_000


def test_contract_type_mapping():
    assert cf.contract_type_of("35+ Contract") == ("standard", ["35_plus"])
    assert cf.contract_type_of("Professional Tryout") == ("PTO", [])
    assert cf.contract_type_of("Standard Contract") == ("standard", [])
    assert cf.contract_type_of("Mystery Contract") == ("standard", ["unrecognized_contract_type"])


def test_browse_page():
    rows = cf.parse_browse_page(read("browse-active-20240704225129.html"))
    assert len(rows) == 50
    mc = next(r for r in rows if r["player_path"] == "/players/connor-mcdavid")
    assert mc == {"name": "Connor McDavid", "player_path": "/players/connor-mcdavid", "team": "EDM",
                  "clause": "NMC", "expiry_status": "UFA", "cap_hit": 12_500_000, "salary": 10_000_000}


# -- rows --------------------------------------------------------------------

def test_to_rows(team_rows, mcdavid, bourgault):
    claims, seasons, clauses, sources = cf.to_rows(team_rows, [mcdavid, bourgault])
    by_name = {}
    for c in claims:
        by_name.setdefault(c.player_name, []).append(c)

    ext, elc = by_name["Connor McDavid"]  # no third claim from the team-page row
    assert ext.signed_at == "2017-07-05" and ext.signing_date_stated and ext.contract_type == "extension"
    assert (ext.term_years, ext.total_usd, ext.cap_hit_usd, ext.aav_usd) == (8, 100_000_000, 12_500_000, 12_500_000)
    assert (ext.start_season, ext.end_season, ext.team, ext.player_id) == ("2018-19", "2025-26", "EDM", None)
    assert ext.contract_key == "connor mcdavid|EDM|2017-07" and ext.confidence_tier == "E"
    assert ext.source_id == f"wayback:{MCDAVID_TS}:/players/connor-mcdavid"
    assert ext.source_url == snap(MCDAVID_TS, "/players/connor-mcdavid")
    assert ext.publisher == "CapFriendly (Internet Archive snapshot)"
    assert ext.published_at == "2024-07-05T18:49:05+00:00" and ext.expiry_status == "UFA"
    assert ext.evidence.startswith("Connor McDavid signed a 8 year") and ext.one_way is True
    assert "varying_cap_hit" not in ext.flags
    assert (elc.term_years, elc.total_usd, elc.cap_hit_usd, elc.signed_at) == (3, 11_325_000, 925_000, "2015-07-03")
    assert elc.contract_type == "ELC" and "total_includes_performance_bonuses" in elc.flags and elc.one_way is False

    ext_seasons = [s for s in seasons if s.claim_id == ext.claim_id]
    assert len(ext_seasons) == 8 and all(s.source_id == ext.source_id for s in ext_seasons)
    s1819 = next(s for s in ext_seasons if s.season == "2018-19")
    assert s1819.base_salary_usd == 2_000_000 and s1819.signing_bonus_usd == 13_000_000
    nmc = [c for c in clauses if c.claim_id == ext.claim_id]
    assert [(c.kind, c.season_from, c.season_to) for c in nmc] == [("NMC", "2022-23", "2025-26")]

    # Bourgault: player page wins, and his two-way team cell agrees once read as the minors salary
    assert len(by_name["Xavier Bourgault"]) == 1
    assert len([s for s in seasons if s.contract_key.startswith("xavier bourgault")]) == 3

    (d,) = by_name["Leon Draisaitl"]  # team-page-only row
    assert d.signed_at == "2024-07-04" and not d.signing_date_stated
    assert {"signing_date_unknown", "term_is_remaining_seasons"} <= set(d.flags)
    assert (d.term_years, d.cap_hit_usd, d.start_season, d.end_season) == (1, 8_500_000, "2024-25", "2024-25")
    assert d.total_usd is None and d.team == "EDM" and d.expiry_status == "UFA"
    d_clauses = {c.kind: c for c in clauses if c.claim_id == d.claim_id}
    assert set(d_clauses) == {"NMC", "M-NTC"}
    assert d_clauses["M-NTC"].list_size == 10 and d_clauses["NMC"].list_size is None
    assert "10 team trade list" in d_clauses["M-NTC"].evidence

    hyman = {(c.kind, c.season_from, c.season_to): c for c in clauses
             if c.claim_id == by_name["Zach Hyman"][0].claim_id}
    assert set(hyman) == {("NMC", "2024-25", "2025-26"), ("M-NTC", "2026-27", "2027-28")}
    assert hyman[("M-NTC", "2026-27", "2027-28")].list_size is None  # two sizes stated (8, then 10)
    janmark = next(c for c in clauses if c.contract_key.startswith("mattias janmark"))
    assert janmark.list_size == 10 and janmark.list_type == "no-trade"

    assert "retained_salary_adjusted" in by_name["Mattias Ekholm"][0].flags
    assert "35_plus" in by_name["Corey Perry"][0].flags
    assert "Dylan Holloway" not in by_name  # no season with money, nothing to claim
    akey = by_name["Beau Akey"][0]
    assert akey.one_way is False and akey.term_years == 3
    akey_season = next(s for s in seasons if s.claim_id == akey.claim_id)
    assert akey_season.minors_salary_usd == 82_500 and akey_season.base_salary_usd is None

    assert {s.source_id for s in sources} == {f"wayback:{MCDAVID_TS}:/players/connor-mcdavid",
                                             f"wayback:{BOURGAULT_TS}:/players/xavier-bourgault",
                                             f"wayback:{TEAM_TS}:/teams/oilers"}
    assert all(s.kind == "wayback_capfriendly" and s.confidence_tier == "E" for s in sources)
    assert len({c.claim_id for c in claims}) == len(claims)


def test_to_rows_keeps_disagreeing_team_season(team_rows, mcdavid):
    rows = copy.deepcopy(team_rows)
    row(rows, "Connor McDavid")["seasons"][0]["base_salary"] = 3_100_000  # pretend the team page disagrees
    claims, seasons, _clauses, _sources = cf.to_rows(rows, [mcdavid])
    assert len([c for c in claims if c.player_name == "Connor McDavid"]) == 2
    s2425 = [s for s in seasons if s.contract_key == "connor mcdavid|EDM|2017-07" and s.season == "2024-25"]
    assert sorted(s.base_salary_usd for s in s2425) == [3_000_000, 3_100_000]
    assert {s.source_id for s in s2425} == {f"wayback:{MCDAVID_TS}:/players/connor-mcdavid",
                                            f"wayback:{TEAM_TS}:/teams/oilers"}


def test_to_rows_dedupes_same_row_on_two_team_pages(team_rows):
    other = [{**r, "snapshot_ts": "20240501000000", "snapshot_url": snap("20240501000000", "/teams/oilers")}
             for r in team_rows]
    claims, *_ = cf.to_rows(team_rows + other, [])
    assert len([c for c in claims if c.player_name == "Leon Draisaitl"]) == 1


# -- collector ---------------------------------------------------------------

class FakeWayback:
    """Answers CDX queries and id_ snapshot fetches from fixtures."""

    def __init__(self, cdx, pages, fail=0):
        self.cdx = cdx          # bare url -> CDX JSON payload
        self.pages = pages      # (ts, path) -> html
        self.calls = []
        self.fail = fail

    def get_bytes(self, url, params=None, *, ok_statuses=(200,)):
        self.calls.append((url, params))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("connection refused")
        if url == wc.CDX:
            payload = self.cdx.get(params["url"], [["timestamp", "original", "length"]])
            return 200, json.dumps(payload).encode(), url
        for (ts, path), html in self.pages.items():
            if url == f"https://web.archive.org/web/{ts}id_/https://www.capfriendly.com{path}":
                return 200, html.encode(), url
        return 404, b"not archived", url


def cdx_row(ts, path, length):
    return [ts, f"https://www.capfriendly.com{path}", str(length)]


def fake_archive():
    header = ["timestamp", "original", "length"]
    oilers = json.loads(read("cdx-teams-oilers-2024.json"))
    cdx = {
        "capfriendly.com/teams/oilers": oilers,
        # only post-shutdown captures: the closing page is all there is
        "capfriendly.com/teams/bruins": [header, cdx_row("20240913053656", "/teams/bruins", 3511)],
        "capfriendly.com/players/connor-mcdavid": [header, cdx_row("20240301000000", "/players/connor-mcdavid", 19000),
                                                   cdx_row(MCDAVID_TS, "/players/connor-mcdavid", 19500),
                                                   cdx_row("20240920000000", "/players/connor-mcdavid", 3500)],
        "capfriendly.com/players/xavier-bourgault": [header, cdx_row(BOURGAULT_TS, "/players/xavier-bourgault",
                                                                     15069)],
    }
    closing = read("teams-oilers-closing-20240913053656.html")
    pages = {
        ("20240709053648", "/teams/oilers"): read(f"teams-oilers-{TEAM_TS}.html"),
        ("20240913053656", "/teams/bruins"): closing,
        (MCDAVID_TS, "/players/connor-mcdavid"): read(f"players-connor-mcdavid-{MCDAVID_TS}.html"),
        (BOURGAULT_TS, "/players/xavier-bourgault"): read(f"players-xavier-bourgault-{BOURGAULT_TS}.html"),
    }
    return FakeWayback(cdx, pages)


def test_rank_snapshots_prefers_final_window():
    rows = wc.parse_cdx(json.loads(read("cdx-teams-oilers-2024.json")))
    ranked = [r[0] for r in wc.rank_snapshots(rows)]
    assert ranked[:3] == ["20240709053648", "20240705220735", "20240704221225"]
    assert ranked[-2:] == ["20241003110219", "20240913053656"]  # post-shutdown closing pages last
    # nothing in the final window: latest spring capture, big ones before small ones
    spring = [("20240610000000", "u", 15000), ("20240620000000", "u", 4000), ("20240301000000", "u", 15000),
              ("20231201000000", "u", 15000), ("20240901000000", "u", 3500)]
    assert [r[0] for r in wc.rank_snapshots(spring)] == ["20240610000000", "20240620000000", "20240301000000",
                                                         "20240901000000"]


def test_classify():
    assert wc.classify(read(f"teams-oilers-{TEAM_TS}.html"), "team") == "ok"
    assert wc.classify(read(f"players-connor-mcdavid-{MCDAVID_TS}.html"), "player") == "ok"
    assert wc.classify(read("teams-oilers-closing-20240913053656.html"), "team") == "closing-page"
    assert wc.classify(read(f"teams-oilers-{TEAM_TS}.html"), "player") == "error"


def test_collect_and_resume(tmp_path):
    client = fake_archive()
    store = Store(tmp_path / "raw.sqlite")
    sleeps = []
    out = wc.collect(store, client, teams=["oilers", "bruins"], pause=0, sleep=sleeps.append)
    assert sleeps == []  # pacing 0 in tests
    assert store.wayback_page_status("https://www.capfriendly.com/teams/oilers") == "ok"
    assert store.wayback_page_status("https://www.capfriendly.com/teams/bruins") == "closing-page"
    assert store.wayback_page_status("https://www.capfriendly.com/players/connor-mcdavid") == "ok"
    assert store.wayback_page_status("https://www.capfriendly.com/players/leon-draisaitl") == "missing"
    assert out["new"] == 2 + 49 and out["statuses"] == {"ok": 3, "closing-page": 1, "missing": 47}

    oilers = next(store.wayback_pages(kind="team"))
    assert oilers["snapshot_ts"] == "20240709053648"
    assert oilers["snapshot_url"] == snap("20240709053648", "/teams/oilers")
    assert "Years Remaining" in gzip.decompress(oilers["html"]).decode()
    mc = next(p for p in store.wayback_pages(kind="player") if p["url"].endswith("connor-mcdavid"))
    assert mc["snapshot_ts"] == MCDAVID_TS
    assert len(wc.player_urls_from_team_pages(store)) == 49

    claims, *_ = cf.rows_from_store(store)
    assert any(c.player_name == "Connor McDavid" and c.signed_at == "2017-07-05" for c in claims)

    client.calls.clear()
    again = wc.collect(store, client, teams=["oilers", "bruins"], pause=0)
    assert again["new"] == 0 and again["requests"] == 0 and client.calls == []
    runs = store.conn.execute("SELECT collector, new_rows FROM runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("wayback_capfriendly", 51), ("wayback_capfriendly", 0)]


def test_collect_honours_max_pages(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    out = wc.collect(store, fake_archive(), teams=["oilers", "bruins"], max_pages=1, pause=0)
    assert out["new"] == 1 and "max_pages" in out["note"]
    assert store.counts()["wayback_pages"] == 1


def test_pacing_and_backoff():
    clock = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    client = fake_archive()
    client.fail = 2
    wb = wc.Wayback(client, pause=2.5, sleep=sleep, clock=lambda: clock[0])
    assert wb.captures("https://www.capfriendly.com/teams/oilers")
    assert sleeps == [10, 2.5, 30, 2.5]  # back off 10s then 30s, and still space requests 2.5s apart
    wb.captures("https://www.capfriendly.com/teams/bruins")
    assert sleeps[-1] == 2.5 and wb.requests == 4

    dead = fake_archive()
    dead.fail = 99
    wb = wc.Wayback(dead, pause=0, sleep=lambda s: None)
    with pytest.raises(wc.WaybackUnavailable):
        wb.captures("https://www.capfriendly.com/teams/oilers")
    assert len(dead.calls) == 4  # first try + 10s, 30s, 90s back-offs


@pytest.mark.skipif(os.environ.get("NHL_CONTRACTS_LIVE") != "1", reason="live Wayback call; set NHL_CONTRACTS_LIVE=1")
def test_live_wayback_smoke():
    from nhl_contracts.http import Client

    with Client(retries=1, pause=0) as client:
        wb = wc.Wayback(client, pause=2.5)
        row = wc.fetch_page(wb, "https://www.capfriendly.com/teams/oilers", "team", max_attempts=1)
    assert row["status"] == "ok" and "20240625" <= row["snapshot_ts"][:8] <= "20240709"
    rows = cf.parse_team_page(row["html"], row["snapshot_url"], row["snapshot_ts"])
    assert any(r["name"] == "Connor McDavid" for r in rows)


def test_team_row_with_extension_is_split_by_cap_hit():
    from nhl_contracts.normalize.capfriendly import _split_by_cap_hit

    seasons = [{"season": "2024-25", "cap_hit": 5_250_000}, {"season": "2025-26", "cap_hit": 7_250_000},
               {"season": "2026-27", "cap_hit": 7_250_000}, {"season": "2027-28", "cap_hit": 7_250_000}]
    segs = _split_by_cap_hit(seasons)
    assert [[x["season"] for x in seg] for seg in segs] == [["2024-25"], ["2025-26", "2026-27", "2027-28"]]
