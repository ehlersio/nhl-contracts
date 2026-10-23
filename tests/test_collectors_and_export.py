import json
from datetime import date

from conftest import FIXTURES, all_stories

from nhl_contracts.collectors import espn_transactions, nhl_forge
from nhl_contracts.normalize.contracts import normalize_all
from nhl_contracts.publish.export import export
from nhl_contracts.store import Store
from nhl_contracts.teams import fetch_teams


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        for key, payload in self.routes.items():
            if key in url or (params and key in str(params)):
                return payload
        raise RuntimeError(f"unrouted {url} {params}")


def test_forge_candidate_filter_and_story_payload():
    listing = json.loads((FIXTURES / "forge_list_transactions_teamid-12.json").read_text())
    items = listing["items"]
    assert len(items) == 25 and all(nhl_forge.is_candidate(i) for i in items)  # all tagged transactions
    story = json.loads((FIXTURES / "forge_story_marat-khusnutdinov.json").read_text())
    s = nhl_forge.story_from_payload(story, "2026-10-23T00:00:00+00:00")
    assert s.slug == "marat-khusnutdinov-signs-three-year-contract-with-boston-bruins"
    assert s.author_tag == "nhl-created" and "playerid-8482177" in s.tags
    assert "forge-entity" in s.body and "$11.85 million" in s.body
    assert s.url == "https://www.nhl.com/news/marat-khusnutdinov-signs-three-year-contract-with-boston-bruins"


def test_forge_collect_is_idempotent(tmp_path):
    listing = json.loads((FIXTURES / "forge_list_transactions_teamid-12.json").read_text())
    story = json.loads((FIXTURES / "forge_story_marat-khusnutdinov.json").read_text())
    # every slug in the list resolves to the same body payload (renamed) for the test
    routes = {"stories?": listing, "tags.slug": listing}
    for item in listing["items"]:
        routes[f"stories/{item['slug']}"] = {**story, "slug": item["slug"],
                                             "lastUpdatedDate": item.get("lastUpdatedDate")}
    client = FakeClient(routes)
    store = Store(tmp_path / "raw.sqlite")
    first = nhl_forge.collect(store, client, [12])
    assert first["new"] == 25
    second = nhl_forge.collect(store, client, [12])
    assert second["new"] == 0
    assert store.counts()["forge_stories"] == 25
    assert len(list(store.stories())) == 25


def test_espn_collect_dedupes(tmp_path, espn_payload):
    client = FakeClient({"transactions": espn_payload})
    store = Store(tmp_path / "raw.sqlite")
    n1 = espn_transactions.collect(store, client, date(2026, 6, 25), date(2026, 7, 15))
    n2 = espn_transactions.collect(store, client, date(2026, 6, 25), date(2026, 7, 15))
    assert n1["new"] == len(espn_payload["transactions"]) and n2["new"] == 0
    row = next(store.espn_rows())
    assert "logos" not in json.loads(row["raw"])["team"]


def test_fetch_teams_filters_to_standings():
    client = FakeClient({
        "standings/now": json.loads((FIXTURES / "nhl_standings_now.json").read_text()),
        "stats/rest/en/team": json.loads((FIXTURES / "nhl_stats_team.json").read_text()),
    })
    teams = fetch_teams(client)
    abbrevs = {ab for ab, _ in teams.values()}
    assert len(abbrevs) == 32 and {"CAR", "BOS", "UTA", "SEA", "LAK"} <= abbrevs
    assert teams[12][0] == "CAR" and teams[6][0] == "BOS"


def test_export_writes_all_files(tmp_path, espn_payload, teams):
    espn_rows = [{"key": str(i), "date": t["date"], "team_abbrev": t["team"]["abbreviation"],
                  "description": t["description"]} for i, t in enumerate(espn_payload["transactions"])]
    claims, sources, merged, _seasons = normalize_all(all_stories(), espn_rows, teams)
    stamp = export(merged, claims, sources, tmp_path / "dist")
    names = {p.name for p in (tmp_path / "dist").iterdir()}
    expected = {"sources.csv", "timestamp.json"}
    for stem in ("contracts", "claims", "contract_seasons", "season_claims", "clauses", "clause_claims"):
        expected |= {f"{stem}.parquet", f"{stem}.csv", f"{stem}.json"}
    assert names == expected
    import pyarrow.parquet as pq

    table = pq.read_table(tmp_path / "dist" / "contracts.parquet")
    assert table.num_rows == stamp["n_contracts"] == len(merged)
    assert table.schema.field("aav_usd").type == "int64"
    ts = json.loads((tmp_path / "dist" / "timestamp.json").read_text())
    assert ts["license"] == {"data": "CC0-1.0", "code": "MIT"} and ts["n_claims"] == len(claims)


def test_jsonl_and_wayback_roundtrip(tmp_path, espn_payload):
    import gzip

    src = Store(tmp_path / "a.sqlite")
    story = json.loads((FIXTURES / "forge_story_marat-khusnutdinov.json").read_text())
    src.upsert_story(nhl_forge.story_from_payload(story, "2026-10-23T00:00:00+00:00"))
    src.upsert_espn(espn_transactions.normalize_rows(espn_payload), "2026-10-23T00:00:00+00:00")
    src.upsert_bsky_posts([{"uri": "at://did/app.bsky.feed.post/abc", "cid": "c", "author": "puckpedia.com",
                            "created_at": "2026-10-22T01:00:00Z", "text": "The #NHLBruins signed …",
                            "links": ["https://puckpedia.com/signing/10842"], "raw": {"text": "x"}}],
                         "2026-10-23T00:00:00+00:00")
    src.set_players({8482177: ("Marat Khusnutdinov", "BOS")}, "2026-10-23")
    src.upsert_wayback_page("https://www.capfriendly.com/teams/oilers", "team", "20240704221225", "u",
                            gzip.compress(b"<html>Years Remaining</html>"), "ok", "2026-10-23T00:00:00+00:00")
    src.commit()
    counts = src.dump_jsonl(tmp_path / "raw")
    assert counts == {"forge_stories": 1, "espn_transactions": len(espn_payload["transactions"]), "bsky_posts": 1,
                      "players": 1, "teams": 0, "llm_cache": 0}
    first = (tmp_path / "raw" / "forge_stories.jsonl").read_text(encoding="utf-8")
    assert '"tags": [' in first  # JSON columns are expanded, not double-encoded
    assert src.dump_wayback(tmp_path / "wb.sqlite") == 1

    dst = Store(tmp_path / "b.sqlite")
    assert dst.load_jsonl(tmp_path / "raw")["forge_stories"] == 1
    assert dst.load_wayback(tmp_path / "wb.sqlite") == 1
    assert dst.counts() == src.counts()
    s = next(dst.stories())
    assert s.tags == nhl_forge.tag_slugs(story) and "$11.85 million" in s.body
    assert next(dst.bsky_posts("puckpedia.com"))["links"] == ["https://puckpedia.com/signing/10842"]
    assert dst.load_jsonl(tmp_path / "raw")["forge_stories"] == 1 and dst.counts()["forge_stories"] == 1
    assert src.dump_jsonl(tmp_path / "raw") == counts  # a second dump is byte-identical
    assert (tmp_path / "raw" / "forge_stories.jsonl").read_text(encoding="utf-8") == first
