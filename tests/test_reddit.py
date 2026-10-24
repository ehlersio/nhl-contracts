from pathlib import Path

import pytest

from nhl_contracts.collectors import reddit_hockey
from nhl_contracts.normalize import reddit
from nhl_contracts.store import Store

FIX = Path(__file__).parent / "fixtures" / "reddit"
NEW = FIX / "r-hockey-new-2026-10-24.xml"
SEARCH = FIX / "r-hockey-search-signings-2026-10-24.xml"


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_bytes(self, url, params=None, *, ok_statuses=(200,)):
        self.calls.append(url)
        for key, (status, body) in self.routes.items():
            if key in url:
                return status, body, url
        raise AssertionError(f"unexpected url {url}")


def _rows(path):
    return reddit_hockey.parse_feed(path.read_bytes())


@pytest.fixture(scope="module")
def search_rows():
    return _rows(SEARCH)


@pytest.fixture(scope="module")
def by_title(search_rows):
    return {r["title"]: r for r in search_rows}


def _claim(by_title, title):
    claims, seasons, clauses, source = reddit.extract_post(by_title[title])
    assert len(claims) == 1, title
    return claims[0], seasons, clauses, source


def _post(title, source_url="https://x.com/someone/status/1?s=20"):
    return {"id": "t3_test1", "title": title, "created_at": "2026-10-20T15:00:00+00:00",
            "permalink": "https://www.reddit.com/r/hockey/comments/test1/x/", "source_url": source_url}


# --- collector ------------------------------------------------------------------------

def test_parse_feed_reads_atom_entries():
    rows = _rows(NEW)
    assert len(rows) == 100
    first = rows[0]
    assert first["id"] == "t3_1xdw0gc"
    assert first["created_at"].startswith("2026-10-24T")
    assert first["permalink"].startswith("https://www.reddit.com/r/hockey/comments/1xdw0gc/")
    assert first["author"] == "u/leftenant_Dan1"
    assert first["source_url"] == "https://i.redd.it/kgp6rj23jbxh1.png"
    assert "[comments]" in first["raw"]["content"]


def test_parse_feed_outbound_link_and_self_posts(by_title):
    crevier = by_title["Sabres sign Crevier to 4-year extension"]
    assert crevier["source_url"] == "https://www.nhl.com/sabres/news/sabres-louis-crevier-contract-extension"
    assert crevier["permalink"] == ("https://www.reddit.com/r/hockey/comments/1wv25zd/"
                                    "sabres_sign_crevier_to_4year_extension/")
    fantilli = next(r for t, r in by_title.items() if t.startswith("Blue Jackets sign center Adam Fantilli"))
    assert "&utm_medium=" in fantilli["source_url"]  # HTML entities in the href are decoded
    self_post = next(r for t, r in by_title.items() if t.startswith("One-year contract offer sheets"))
    assert self_post["source_url"] is None


def test_contract_title_filter():
    titles = [r["title"] for r in _rows(NEW)]
    kept = [t for t in titles if reddit_hockey.is_contract_title(t)]
    assert 0 < len(kept) <= 5
    assert any("Cap Hit is reduced" in t for t in kept)
    assert not reddit_hockey.is_contract_title("Jake Evans scores his second of the game on the penalty kill")
    assert reddit_hockey.is_contract_title("[Friedman] Debrincat is closing in on a 7 x $13M extension")
    assert reddit_hockey.is_contract_title("Rangers Agree to Terms with Luke Glendening")


def test_collect_stores_contract_posts_idempotently(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    client = FakeClient({"new.rss": (200, NEW.read_bytes()), "search.rss": (200, SEARCH.read_bytes())})
    both = (reddit_hockey.NEW_FEED, reddit_hockey.SEARCH_FEED)  # the search feed is opt-in (429 from cloud runners)
    first = reddit_hockey.collect(store, client, feeds=both, pause=0)
    assert len(client.calls) == 2
    assert first["requests"] == 2 and first["entries"] == 200
    stored = reddit_hockey.posts(store)
    assert first["new"] == len(stored) > 30
    assert all(reddit_hockey.is_contract_title(p["title"]) for p in stored)
    assert len({p["id"] for p in stored}) == len(stored)
    assert isinstance(stored[0]["raw"], dict)
    again = reddit_hockey.collect(store, client, feeds=both, pause=0)
    assert again["new"] == 0
    assert len(reddit_hockey.posts(store)) == len(stored)
    runs = store.conn.execute("SELECT collector, requests, new_rows FROM runs").fetchall()
    assert [tuple(r) for r in runs] == [("reddit_hockey", 2, first["new"]), ("reddit_hockey", 2, 0)]


def test_collect_survives_bad_feeds(tmp_path):
    store = Store(tmp_path / "raw.sqlite")
    client = FakeClient({"new.rss": (429, b""), "search.rss": (200, b"<html>not atom")})
    out = reddit_hockey.collect(store, client, feeds=(reddit_hockey.NEW_FEED, reddit_hockey.SEARCH_FEED), pause=0)
    assert out["new"] == 0 and out["errors"] == 2
    assert reddit_hockey.posts(store) == []


# --- parser: fixture titles ------------------------------------------------------------

def test_khusnutdinov_club_release(by_title):
    c, seasons, clauses, src = _claim(
        by_title, "Bruins Sign Marat Khusnutdinov to Three-Year Contract Extension (3x$3.95m AAV)")
    assert (c.player_name, c.team, c.term_years, c.aav_usd, c.total_usd) == (
        "Marat Khusnutdinov", "BOS", 3, 3_950_000, None)
    assert c.contract_type == "extension"
    assert src.author is None and src.publisher == "r/hockey post"
    assert src.kind == "reddit_hockey" and src.source_id == "reddit:t3_1xbxv3q"
    assert src.url == c.source_url and c.source_url.startswith("https://www.reddit.com/r/hockey/comments/1xbxv3q/")
    assert c.confidence_tier == "D" and c.extracted_by == "regex"
    assert c.signed_at == "2026-10-21" and c.signing_date_stated is False
    assert "ref:https://www.nhl.com/bruins/news/bruins-sign-marat-khusnutdinov-to-three-year-contract-extension" \
        in c.flags
    assert seasons == [] and clauses == []


def test_khusnutdinov_report_total(by_title):
    c, *_ = _claim(by_title, "Report: Bruins signing Khusnutdinov to 3-year, $11.85M extension")
    assert (c.player_name, c.team, c.term_years, c.total_usd, c.aav_usd) == (
        "Khusnutdinov", "BOS", 3, 11_850_000, None)
    assert "report" in c.flags and "surname_only" in c.flags


def test_desmith_total_and_per_year(by_title):
    c, *_ = _claim(by_title, "Dallas Stars sign Goaltender Casey DeSmith to a two-year, $5 million contract "
                             "extension (2x$2.5m AAV)")
    assert (c.player_name, c.team, c.contract_type) == ("Casey DeSmith", "DAL", "extension")
    assert (c.term_years, c.total_usd, c.aav_usd) == (2, 5_000_000, 2_500_000)
    assert "total_vs_aav_mismatch" not in c.flags


def test_marchenko_insider(by_title):
    title = next(t for t in by_title if t.startswith("[Milstein] Kirill Marchenko"))
    c, _, _, src = _claim(by_title, title)
    assert (c.player_name, c.team, c.contract_type, c.term_years, c.total_usd) == (
        "Kirill Marchenko", "TOR", "extension", 6, 75_000_000)
    assert c.aav_usd == 12_500_000
    assert "insider:Milstein" in c.flags and "report" in c.flags
    assert src.author == "Milstein" and src.publisher == "r/hockey post quoting Milstein"


def test_glendening_pto_from_club_account(by_title):
    title = next(t for t in by_title if t.startswith("[NY Rangers PR] The Rangers have signed forward Luke"))
    c, _, _, src = _claim(by_title, title)
    assert (c.player_name, c.team, c.contract_type, c.term_years) == ("Luke Glendening", "NYR", "PTO", None)
    assert src.author == "NY Rangers PR"
    assert "insider:NY Rangers PR" in c.flags and "report" not in c.flags


def test_crevier_surname_only(by_title):
    c, *_ = _claim(by_title, "Sabres sign Crevier to 4-year extension")
    assert (c.player_name, c.team, c.term_years, c.contract_type) == ("Crevier", "BUF", 4, "extension")
    assert "surname_only" in c.flags


def test_rumour_yields_no_claim(by_title):
    title = "[Elliotte Friedman] Debrincat is closing in on a 7 x $13M extension with Detroit"
    assert reddit.classify_title(title) == "rumour"
    assert reddit.extract_post(by_title[title]) == ([], [], [], None)
    karlsson = next(t for t in by_title if "closing in on a contract extension with William Karlsson" in t)
    assert reddit.classify_title(karlsson) == "rumour"


@pytest.mark.parametrize("start", [
    "[PuckPedia] The Red Wings signed 28 y/o F Alex Debrincat",        # PuckPedia is read directly
    "[Puckpedia] The Canes signed 38 y/o F Jordan Staal",
    "PWHL Las Vegas Signs Fifth Overall Pick Lacey Eden",
    "Evgeny Kuznetzov has KHL contract mutually terminated",
    "[Genève-Servette HC] UFA Adam Boqvist has signed",
    "Curtis Lazar signs 1 year deal with Lausanne HC",
    "HC Ajoie signs forward Logan Brown",
    "The Hartford Wolf Pack has signed goaltender James Reimer",
    "SHL team HV71 signs a one year contract",
    "The Vancouver Goldeneyes sign first overall draft pick",
    "Ottawa Senators sign Travis Green",                                # coach, "multi-year" only
    "[Sharks] San Jose Sharks Head Coach Ryan Warsofsky",
    "[Kaplan] Bill Guerin has signed",
    "Sens first EBUG signing press conference",
    "John Forslund Signs New Multi-Year Deal",
    "Cutter Gauthier scores his first goal of the season fresh off his extension",
    "[Lowlight] John Buccigross states",
    "[Bruce Garrioch] Defenceman Ben Hutton is being released from the PTO",
    "TSN Insider Trading - 'Brisson and Guerin remain in contact BUT Quinn Hughes has NOT",
    "When’s the last time an NHL superstar was traded to or signed",
    "[Foley] Greg Powers’ role was Arizona State’s",
    "Cutter signed 6 Years",                                            # no team in the title
])
def test_non_claims(by_title, start):
    title = next(t for t in by_title if t.startswith(start))
    assert reddit.extract_post(by_title[title]) == ([], [], [], None), title


def test_fixture_claim_counts(search_rows):
    claims, seasons, clauses, sources = reddit.normalize_posts(search_rows)
    assert len(claims) == len(sources) == 20
    assert {c.source_id for c in claims} == {s.source_id for s in sources}
    names = {c.player_name for c in claims}
    assert {"Luke Evangelista", "Ethan Del Mastro", "Garrett Wilson", "Adam Fantilli", "Perron"} <= names
    new_claims, *_ = reddit.normalize_posts(_rows(NEW))
    assert new_claims == []


def test_more_fixture_values(by_title):
    evangelista = next(t for t in by_title if t.startswith("Luke Evangelista Inks"))
    c, *_ = _claim(by_title, evangelista)
    assert (c.team, c.term_years, c.total_usd, c.aav_usd) == ("NJD", 5, 36_250_000, 7_250_000)
    c, *_ = _claim(by_title, "Flames Sign Alex Nylander (1 year, two-way contract; $850k AAV)")
    assert (c.team, c.term_years, c.aav_usd, c.one_way) == ("CGY", 1, 850_000, False)
    fantilli = next(t for t in by_title if t.startswith("Blue Jackets sign center Adam Fantilli"))
    c, *_ = _claim(by_title, fantilli)
    assert (c.team, c.term_years, c.start_season, c.end_season) == ("CBJ", 6, "2026-27", "2031-32")
    assert "ref:https://www.nhl.com/bluejackets/news/fantilli-contract-signed" in c.flags
    c, *_ = _claim(by_title, "Canes Sign Kochetkov To Two-Year Extension | Carolina Hurricanes")
    assert (c.player_name, c.team, c.term_years) == ("Kochetkov", "CAR", 2)


# --- parser: synthetic titles -------------------------------------------------------------

def test_cap_hit_kept_apart_from_aav():
    c, *_ = reddit.extract_post(_post("[Friedman] Kings sign Jordan Spence to 3-year, $4.5M cap hit contract"))[0]
    assert (c.cap_hit_usd, c.aav_usd, c.total_usd, c.term_years) == (4_500_000, None, None, 3)
    assert "ref:https://x.com/someone/status/1" in c.flags  # tracking parameter dropped


def test_per_year_form_is_not_a_total():
    [c], *_ = reddit.extract_post(_post("Leafs re-sign Matthew Knies to 6 x $7.75M deal"))
    assert (c.term_years, c.aav_usd, c.total_usd, c.re_signed, c.team) == (6, 7_750_000, None, True, "TOR")


def test_clauses_and_seasons_only_when_stated():
    claims, seasons, clauses, _ = reddit.extract_post(_post(
        "[Seravalli] Kings have signed Quinton Byfield to a two year extension "
        "Yr 1: $1.5M salary Yr 2: $2.5M salary, 10-team NTC"))
    assert [s.base_salary_usd for s in seasons] == [1_500_000, 2_500_000]
    assert [(k.kind, k.list_size) for k in clauses] == [("M-NTC", 10)]
    assert claims[0].start_season == seasons[0].season
    _, seasons, clauses, _ = reddit.extract_post(_post("[Friedman] Oilers sign Zach Hyman to a 3-year, $6M AAV "
                                                       "extension. Includes full NMC"))
    assert seasons == [] and [k.kind for k in clauses] == ["NMC"]


def test_skips_off_limits_links():
    assert reddit.extract_post(_post("Kings sign Jordan Spence to 3-year, $4.5M cap hit contract",
                                     source_url="https://puckpedia.com/player/jordan-spence")) == ([], [], [], None)


def test_classify_title():
    assert reddit.classify_title("Sabres sign Crevier to 4-year extension") == "signing"
    assert reddit.classify_title("[Kaplan]: The Vegas Golden Knights are closing in on a contract extension") \
        == "rumour"
    assert reddit.classify_title("Is Patrik Laine's NHL career over? He signed a contract") == "other"
    assert reddit.classify_title("Oilers' Formenton signing draws pushback from fans") == "other"
    assert reddit.classify_title("[COL (2) - TBL 0] Kadri signs a stick for a fan") == "other"
