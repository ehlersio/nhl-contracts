# ruff: noqa: E501  -- real PuckPedia posts and ESPN rows are quoted whole
"""LTIR: the ESPN injury-list collector and the stints joined from ESPN's list,
ESPN's transaction wire and PuckPedia's posts."""

import json

from nhl_contracts.collectors import espn_injuries as inj
from nhl_contracts.normalize import ltir
from nhl_contracts.publish.export import export
from nhl_contracts.store import Store


def _injury(aid, name, team, fantasy, date="2026-11-01T12:00Z", status="Injured Reserve"):
    return {"id": f"i{aid}", "status": status, "date": date, "shortComment": f"{name} is out.",
            "type": {"abbreviation": "IR"}, "details": {"fantasyStatus": {"abbreviation": fantasy}},
            "athlete": {"displayName": name, "team": {"id": "1", "abbreviation": team, "logos": [{"href": "x"}]},
                        "links": [{"href": f"https://www.espn.com/nhl/player/_/id/{aid}"}],
                        "headshot": {"href": "x"}}}


def _payload(*injuries):
    return {"timestamp": "x", "injuries": [{"id": "1", "displayName": "Team", "injuries": list(injuries)}]}


def _post(rkey, created_at, text):
    return {"uri": f"at://did:plc:x/app.bsky.feed.post/{rkey}", "author": "puckpedia.com", "created_at": created_at,
            "text": text, "links": []}


def _txn(key, date, team, desc):
    return {"key": key, "date": date, "team_abbrev": team, "description": desc}


# --- collector ----------------------------------------------------------------------

def test_parse_slims_rows_and_reads_the_espn_id():
    rows = inj.parse(_payload(_injury("3041970", "Aleksander Barkov", "FLA", "IR-LT")))
    assert len(rows) == 1
    r = rows[0]
    assert (r["athlete_id"], r["athlete_name"], r["team_abbrev"], r["fantasy_status"]) == \
        ("3041970", "Aleksander Barkov", "FLA", "IR-LT")
    assert "links" not in r["raw"]["athlete"] and "logos" not in r["raw"]["athlete"]["team"]


def test_snapshots_open_and_close_stints(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    barkov = _injury("1", "Aleksander Barkov", "FLA", "IR-LT")
    shesty = _injury("2", "Igor Shesterkin", "NYR", "IR")
    assert inj.apply_snapshot(store, inj.parse(_payload(barkov, shesty)), "2026-11-08T10:00:00+00:00") == \
        {"listed": 2, "opened": 2, "closed": 0}
    # an hour later: nothing changes, no row is touched
    assert inj.apply_snapshot(store, inj.parse(_payload(barkov, shesty)), "2026-11-08T11:00:00+00:00")["opened"] == 0
    # Shesterkin moves to IR-LT: the IR stint closes, an IR-LT one opens; Barkov leaves the list
    shesty_lt = _injury("2", "Igor Shesterkin", "NYR", "IR-LT")
    assert inj.apply_snapshot(store, inj.parse(_payload(shesty_lt)), "2026-11-09T10:00:00+00:00") == \
        {"listed": 1, "opened": 1, "closed": 2}
    rows = {(r["athlete_name"], r["fantasy_status"]): r for r in store.injuries()}
    assert rows[("Aleksander Barkov", "IR-LT")]["ended_at"] == "2026-11-09T10:00:00+00:00"
    assert rows[("Igor Shesterkin", "IR")]["ended_at"] == "2026-11-09T10:00:00+00:00"
    assert rows[("Igor Shesterkin", "IR-LT")]["ended_at"] is None
    assert rows[("Igor Shesterkin", "IR-LT")]["first_seen"] == "2026-11-09T10:00:00+00:00"
    # one snapshot row per day, the latest
    assert store.latest_injury_snapshot() == "2026-11-09T10:00:00+00:00"
    assert store.counts()["espn_injury_snapshots"] == 2


def test_empty_list_does_not_close_stints(tmp_path):
    class Client:
        def get_json(self, url, params=None):
            return {"injuries": []}

    store = Store(tmp_path / "s.sqlite")
    inj.apply_snapshot(store, inj.parse(_payload(_injury("1", "A B", "FLA", "IR-LT"))), "2026-11-08T10:00:00+00:00")
    assert inj.collect(store, Client())["closed"] == 0
    assert len(store.open_injuries()) == 1


def test_injury_tables_roundtrip_jsonl(tmp_path):
    src = Store(tmp_path / "a.sqlite")
    inj.apply_snapshot(src, inj.parse(_payload(_injury("1", "Aleksander Barkov", "FLA", "IR-LT"))),
                       "2026-11-08T10:00:00+00:00")
    src.commit()
    counts = src.dump_jsonl(tmp_path / "raw")
    assert counts["espn_injuries"] == 1 and counts["espn_injury_snapshots"] == 1
    dst = Store(tmp_path / "b.sqlite")
    dst.load_jsonl(tmp_path / "raw")
    row = next(dst.injuries())
    assert row["raw"]["athlete"]["displayName"] == "Aleksander Barkov" and row["ended_at"] is None
    assert dst.latest_injury_snapshot() == "2026-11-08T10:00:00+00:00"


# --- ESPN transaction wording ----------------------------------------------------------

def test_espn_transaction_events():
    rows = [
        _txn("a", "2026-10-12T07:00Z", "WSH", "Placed D Rasmus Sandin on long-term injured reserve and D Matt Roy on injured reserve."),
        _txn("b", "2026-09-30T07:00Z", "TOR", "Moved C Max Domi from the non-roster list to long-term injured reserve."),
        _txn("c", "2026-02-25T08:00Z", "TOR", "Reinstated F Dakota Joshua from long-term injured reserve."),
        _txn("d", "2024-12-07T08:00Z", "TOR", "Placed D Jake McCabe on injured reserve. Activated LW Max Pacioretty from long-term injured reserve."),
        _txn("e", "2026-10-01T07:00Z", "DAL", "Placed F Matt Duchene on long-term injured reserve retroactive to Sept. 29."),
        _txn("f", "2023-01-01T08:00Z", "NJ", "Transferred D Jonas Siegenthaler from injured reserve to long-term injured reserve."),
        _txn("g", "2023-01-02T08:00Z", "BUF", "Reinstated F Jeff Skinner from long-term injured reserve and D Owen Power from long-term injured reserve."),
        _txn("h", "2026-10-26T07:00Z", "CAR", "Assigned F Skyler Brind'Amour to Chicago (AHL). Reinstated F Taylor Hall from injured reserve."),
        _txn("i", "2020-01-01T08:00Z", "NYR", "Recalled G Igor Shesterkin from long-term IR at Hartford (AHL)."),
        _txn("j", "2026-10-02T07:00Z", "BOS", "Signed D Travis Dermott to a one-year contract."),
    ]
    events, sources = ltir.espn_transaction_events(rows)
    got = {(e.kind, e.team, e.name) for e in events}
    assert got == {
        ("place", "WSH", "Rasmus Sandin"),
        ("place", "TOR", "Max Domi"),
        ("activate", "TOR", "Dakota Joshua"),
        ("activate", "TOR", "Max Pacioretty"),
        ("place", "DAL", "Matt Duchene"),
        ("place", "NJD", "Jonas Siegenthaler"),
        ("activate", "BUF", "Jeff Skinner"),
        ("activate", "BUF", "Owen Power"),
        ("activate_ir", "CAR", "Taylor Hall"),
    }
    assert next(e for e in events if e.name == "Matt Duchene").flags == ["retroactive"]
    assert {s.source_id for s in sources} == {f"espn:{k}" for k in "abcdefgh"}


# --- PuckPedia wording -------------------------------------------------------------------

SANDIN = ("The #ALLCAPS put Sandin on LTIR when they had $125K of cap space.\n\nThey can exceed the cap by $3.85M.\n\n"
          "After calling up Holl, they have $3.1M of space remaining in LTIR\n\nhttps://puckpedia.com/team/washington-capitals?6acd292510365")
DOMI_JOSHUA = ("The #Leafs have added both Max Domi ($3.75M) & Dakota Joshua ($3.25M) to LTIR, gaining their $7M Cap Hit "
               "as additional Cap Space in LTIR.\n\nhttps://puckpedia.com/team/toronto-maple-leafs?6abd7860805b0")
PRICE_LAINE = ("The #GoHabsGo added Price ($10.5M) to LTIR & activated Laine ($8.7M) from LTIR.\n\n"
               "They have $10.2M Cap Space remaining in LTIR\n\npuckpedia.com/team/montrea...")
ROSTER_LINE = ("The #LeafsForever moved Pacioretty to LTIR.  They then used the space to call up Minten.\n\n"
               "They have $789K Cap Space remaining in LTIR with 22 active players.\n\nLTIR: Jarnkrok, Pacioretty, Mermis\n"
               "IR: Matthews, Kampf\nSUSP: Reaves\n\npuckpedia.com/team/toronto...")
CONDITIONAL_POST = ("The #TexasHockey have $4.6M Cap Space available in LTIR with Seguin & Lundkvist on LTIR.\n\n"
                    "If they add Heiskanen to LTIR, they would have $13M available.\n\nhttps://puckpedia.com/team/dallas-stars?67ca1b1d00c8e")
BJUGSTAD = ("In case you were curious, #NJDevils have $1.33M of current cap space. They can also utilize $1.5M of LTIR as well "
            "with Bjugstad on LTIR.\n\nhttps://puckpedia.com/team/new-jersey-devils?6adb6ce05294d")
MULTI_TEAM = ("This week, #NYR #MNWild & #NJDevils placed players on LTIR.\n\nWe now have 10 teams with players on LTIR so far this season.")
EXITED = "The #LetsGoPens activated Nieto & exited LTIR.\n\nThey have $845K Projected Cap Space with 23 active players."


def _pp(text):
    return ltir.puckpedia_events([_post("r1", "2026-10-12T19:00:50Z", text)])[0]


def test_puckpedia_placement_with_relief():
    events = _pp(SANDIN)
    assert [(e.kind, e.team, e.name, e.relief, e.relief_shared) for e in events] == \
        [("place", "WSH", "Sandin", 3_850_000, False)]


def test_puckpedia_two_players_share_one_stated_figure():
    events = _pp(DOMI_JOSHUA)
    assert {(e.name, e.relief, e.relief_shared) for e in events} == \
        {("Max Domi", 7_000_000, True), ("Dakota Joshua", 7_000_000, True)}


def test_puckpedia_place_and_activate_in_one_sentence():
    assert {(e.kind, e.name) for e in _pp(PRICE_LAINE)} == {("place", "Price"), ("activate", "Laine")}
    assert all(e.relief is None for e in _pp(PRICE_LAINE))   # "remaining in LTIR" is not relief


def test_puckpedia_roster_line_and_conditionals():
    got = {(e.kind, e.name) for e in _pp(ROSTER_LINE)}
    assert got == {("place", "Pacioretty"), ("listed", "Jarnkrok"), ("listed", "Pacioretty"), ("listed", "Mermis")}
    got = {(e.kind, e.name) for e in _pp(CONDITIONAL_POST)}
    assert got == {("listed", "Seguin"), ("listed", "Lundkvist")}   # "If they add Heiskanen" is not a placement


def test_puckpedia_listing_carries_relief_when_nobody_is_placed():
    assert [(e.kind, e.name, e.relief) for e in _pp(BJUGSTAD)] == [("listed", "Bjugstad", 1_500_000)]


def test_puckpedia_skips_multi_team_posts_and_reads_exits():
    assert _pp(MULTI_TEAM) == []
    assert [(e.kind, e.name) for e in _pp(EXITED)] == [("activate", "Nieto")]


# --- stints ---------------------------------------------------------------------------------

PLAYERS = {
    8480873: ("Rasmus Sandin", "WSH"), 8477503: ("Max Domi", "TOR"), 8478057: ("Dakota Joshua", "TOR"),
    8475760: ("Nick Bjugstad", "WPG"), 8478109: ("Victor Olofsson", "VGK"), 8478450: ("Parker Wotherspoon", "VGK"),
    8477493: ("Aleksander Barkov", "FLA"), 8476792: ("Torey Krug", "STL"),
}


def _stints(txns, posts, payload_rows, as_of="2026-11-08T10:00:00+00:00", tmp_path=None):
    store = Store(tmp_path / "s.sqlite")
    if payload_rows is not None:
        inj.apply_snapshot(store, inj.parse(_payload(*payload_rows)), as_of)
    rows, sources = ltir.normalize(txns, posts, list(store.injuries()), store.latest_injury_snapshot(), PLAYERS, [],
                                   as_of=as_of)
    return rows, sources


def test_stints_join_sources(tmp_path):
    txns = [
        _txn("a", "2026-10-12T07:00Z", "WSH", "Placed D Rasmus Sandin on long-term injured reserve."),
        _txn("b", "2026-10-03T07:00Z", "FLA", "Placed C Aleksander Barkov on long-term injured reserve."),
        _txn("c", "2024-10-08T07:00Z", "STL", "Placed D Torey Krug on long-term injured reserve."),
        _txn("d", "2025-01-08T07:00Z", "VGK", "Placed F Victor Olofsson on long-term injured reserve."),
    ]
    posts = [
        _post("s1", "2026-10-12T19:00:50Z", SANDIN),
        _post("o1", "2026-10-11T22:00:49Z", "The #VegasBorn added Olofsson to LTIR, gaining his $1.64M cap hit as available space."),
        _post("w1", "2026-10-22T18:00:50Z", "The #VegasBorn added Wotherspoon to LTIR, gaining his $500K cap hit as space."),
        _post("b1", "2026-10-23T18:00:50Z", BJUGSTAD),
    ]
    listed = [
        _injury("10", "Rasmus Sandin", "WSH", "IR-LT"),
        _injury("11", "Aleksander Barkov", "FLA", "IR-LT"),
        _injury("12", "Torey Krug", "STL", "IR-LT"),
        _injury("13", "Nick Bjugstad", "WPG", "IR-LT"),
        _injury("14", "Parker Wotherspoon", "VGK", "IR"),
    ]
    rows, sources = _stints(txns, posts, listed, tmp_path=tmp_path)
    by = {(r["player_name"], r["team"], r["season"]): r for r in rows}

    sandin = by[("Rasmus Sandin", "WSH", "2026-27")]
    assert sandin["active"] and sandin["player_id"] == 8480873
    assert (sandin["since"], sandin["since_basis"]) == ("2026-10-12", "espn_transactions")
    assert (sandin["stated_relief_usd"], sandin["stated_at"]) == (3_850_000, "2026-10-12")
    assert sandin["stated_source"] == "bsky:s1" and sandin["conflicts"] == ""
    assert set(sandin["sources"].split(";")) == {"espn:a", "bsky:s1", next(
        s.source_id for s in sources if s.kind == "espn_injuries" and "Sandin" in s.headline)}

    # a stint from an earlier season that ESPN still lists stays active
    krug = by[("Torey Krug", "STL", "2024-25")]
    assert krug["active"] and krug["since"] == "2024-10-08" and krug["until"] is None

    # PuckPedia says LTIR, ESPN's list disagrees: active, with the conflict named
    olofsson = by[("Victor Olofsson", "VGK", "2026-27")]
    assert olofsson["active"] and olofsson["conflicts"] == "espn_not_listed"
    assert olofsson["since_basis"] == "bluesky_puckpedia" and olofsson["stated_relief_usd"] == 1_640_000
    wotherspoon = by[("Parker Wotherspoon", "VGK", "2026-27")]
    assert wotherspoon["active"] and wotherspoon["conflicts"] == "espn_status:IR"

    # last season's open stint nothing closed is history, not active
    old = by[("Victor Olofsson", "VGK", "2024-25")]
    assert not old["active"] and "end_not_found" in old["flags"]

    # PuckPedia has Bjugstad with NJD, ESPN's later list with WPG: NJD ends, WPG is active, both flagged
    njd = by[("Nick Bjugstad", "NJD", "2026-27")]
    wpg = by[("Nick Bjugstad", "WPG", "2026-27")]
    assert not njd["active"] and njd["until_basis"] == "later_team" and njd["conflicts"] == "team:NJD|WPG"
    assert wpg["active"] and wpg["conflicts"] == "team:NJD|WPG" and wpg["since"] is None
    assert wpg["first_seen"] == "2026-11-08"


def test_activations_close_stints(tmp_path):
    txns = [
        _txn("a", "2025-12-01T08:00Z", "TOR", "Placed F Dakota Joshua on long-term injured reserve."),
        _txn("b", "2026-02-25T08:00Z", "TOR", "Activated F Dakota Joshua from injured reserve."),
        _txn("c", "2026-02-27T08:00Z", "TOR", "Activated F Max Domi from injured reserve."),   # never on LTIR: no row
    ]
    rows, _ = _stints(txns, [], None, as_of="2026-03-01", tmp_path=tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["player_name"] == "Dakota Joshua" and not r["active"]
    assert (r["since"], r["until"], r["until_basis"]) == ("2025-12-01", "2026-02-25", "espn_transactions")
    assert "activation_from_ir" in r["flags"]


def test_ambiguous_surname_is_not_guessed(tmp_path):
    players = {1: ("Jack Hughes", "NJD"), 2: ("Luke Hughes", "NJD")}
    rows, _ = ltir.normalize([], [_post("h", "2025-03-05T22:00:13Z", "With #NJDevils adding Hughes to LTIR.  They can add more.")],
                             [], None, players, [], as_of="2025-03-06")
    assert rows[0]["player_name"] == "Hughes" and rows[0]["player_id"] is None
    assert "name_unresolved" in rows[0]["flags"]


def test_export_writes_ltir(tmp_path):
    rows, _ = _stints([_txn("a", "2026-10-12T07:00Z", "WSH", "Placed D Rasmus Sandin on long-term injured reserve.")],
                      [], [], tmp_path=tmp_path)
    stamp = export([], [], [], tmp_path / "dist", ltir=rows)
    assert stamp["n_ltir"] == 1 and stamp["n_ltir_active"] == 1
    out = json.loads((tmp_path / "dist" / "ltir.json").read_text())
    assert out[0]["player_id"] == 8480873 and out[0]["active"] is True
    import pyarrow.parquet as pq

    t = pq.read_table(tmp_path / "dist" / "ltir.parquet")
    assert t.schema.field("active").type == "bool" and t.schema.field("stated_relief_usd").type == "int64"
