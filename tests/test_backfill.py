"""Backfill by player id: roster/prospect ids and per-player Forge lists (no live calls)."""

import json

from conftest import FIXTURES

from nhl_contracts.collectors import nhl_forge
from nhl_contracts.config import STATIC_TEAMS
from nhl_contracts.store import Store
from nhl_contracts.teams import fetch_player_ids


class FakeClient:
    """Routes by exact URL, then by the ``tags.slug`` query; anything else raises like the real client."""

    def __init__(self, urls=None, lists=None):
        self.urls = urls or {}
        self.lists = lists or {}
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        if params and "tags.slug" in params:
            if params["tags.slug"] in self.lists:
                return self.lists[params["tags.slug"]]
        elif url in self.urls:
            payload = self.urls[url]
            if isinstance(payload, Exception):
                raise payload
            return payload
        raise RuntimeError(f"unrouted {url} {params}")


def person(pid, first, last):
    return {"id": pid, "firstName": {"default": first}, "lastName": {"default": last}, "positionCode": "C"}


def standings_teams():
    """{team_id: abbrev} for the standings fixture's 32 clubs, plus Utah's second id."""
    abbrevs = {s["teamAbbrev"]["default"] for s in
               json.loads((FIXTURES / "nhl_standings_now.json").read_text())["standings"]}
    teams = {tid: ab for tid, ab in STATIC_TEAMS.items() if ab in abbrevs}
    assert len(teams) == 32
    return {**teams, 68: "UTA"}


ROSTER = "https://api-web.nhle.com/v1/roster/{}/current"
PROSPECTS = "https://api-web.nhle.com/v1/prospects/{}"


def test_fetch_player_ids_rosters_and_prospects():
    teams = standings_teams()
    urls = {}
    for ab in set(teams.values()):
        urls[ROSTER.format(ab)] = {"forwards": [], "defensemen": [], "goalies": []}
        urls[PROSPECTS.format(ab)] = {"forwards": [], "defensemen": [], "goalies": []}
    urls[ROSTER.format("CAR")] = {"forwards": [person(8478427, "Sebastian", "Aho")],
                                  "defensemen": [], "goalies": [person(8481611, "Pyotr", "Kochetkov")]}
    urls[PROSPECTS.format("CAR")] = {"forwards": [], "defensemen": [person(8485000, "William", "Håkansson")],
                                     "goalies": [person(8481611, "Pyotr", "Kochetkov")]}
    urls[ROSTER.format("UTA")] = {"forwards": [person(8483000, "Joshua", "Roy")]}
    urls[PROSPECTS.format("BOS")] = RuntimeError("503")  # a failed list is skipped
    client = FakeClient(urls=urls)
    ids = fetch_player_ids(client, teams)
    assert ids == {8478427: ("Sebastian Aho", "CAR"), 8481611: ("Pyotr Kochetkov", "CAR"),
                   8485000: ("William Håkansson", "CAR"), 8483000: ("Joshua Roy", "UTA")}
    fetched = [url for url, _ in client.calls]
    assert len(fetched) == 64 and len(set(fetched)) == 64  # 32 clubs x 2 lists; Utah's second id skipped


def forge_routes():
    listing = json.loads((FIXTURES / "forge_list_transactions_teamid-12.json").read_text())
    story = json.loads((FIXTURES / "forge_story_marat-khusnutdinov.json").read_text())
    bodies = {f"{nhl_forge.FORGE_STORIES}/{item['slug']}": {**story, "slug": item["slug"],
                                                            "lastUpdatedDate": item.get("lastUpdatedDate")}
              for item in listing["items"]}
    return listing, bodies


def test_backfill_players_is_idempotent_and_marks_seen(tmp_path):
    listing, bodies = forge_routes()
    half = {**listing, "items": listing["items"][:10]}
    client = FakeClient(urls=bodies, lists={"playerid-8482177": listing, "playerid-8481611": half})
    store = Store(tmp_path / "raw.sqlite")
    first = nhl_forge.backfill_players(store, client, [8482177, 8481611, 8482177])
    assert first == {"requests": 2 + 25, "new": 25, "listed": 25, "players": 2}
    second = nhl_forge.backfill_players(store, client, [8482177, 8481611])
    assert second == {"requests": 2, "new": 0, "listed": 25, "players": 2}
    assert store.counts()["forge_stories"] == 25
    queries = {r[0] for r in store.conn.execute("SELECT DISTINCT list_query FROM forge_seen")}
    assert queries == {"playerid-8482177"}  # first query a slug was seen under
    runs = store.conn.execute("SELECT collector, requests, new_rows, note FROM runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("nhl_forge_players", 27, 25, "players=2"),
                                        ("nhl_forge_players", 2, 0, "players=2")]


def test_backfill_players_caps_and_survives_failed_lists(tmp_path):
    listing, bodies = forge_routes()
    client = FakeClient(urls=bodies, lists={"playerid-2": listing})
    store = Store(tmp_path / "raw.sqlite")
    out = nhl_forge.backfill_players(store, client, [1, 2, 3], max_players=2)  # 1 fails, 3 is never polled
    assert out["players"] == 2 and out["new"] == 25
    polled = [p["tags.slug"] for _, p in client.calls if p]
    assert polled == ["playerid-1", "playerid-2"]


def test_collect_still_counts_like_before(tmp_path):
    listing, bodies = forge_routes()
    lists = {q: listing for q in nhl_forge.list_queries([12])}
    client = FakeClient(urls=bodies, lists=lists)
    store = Store(tmp_path / "raw.sqlite")
    assert nhl_forge.collect(store, client, [12]) == {"requests": 4 + 25, "new": 25, "listed": 25}
    assert nhl_forge.collect(store, client, [12]) == {"requests": 4, "new": 0, "listed": 25}
