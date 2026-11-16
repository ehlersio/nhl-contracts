"""Name -> NHL player id, namesakes kept apart (normalize/identity.py).

Regression for 2026-11-16: the CAR centre Sebastian Aho (8478427) carried the
contracts of the defenceman Sebastian Aho (8480222, NYI 2017-24, PIT 2024-26),
because the defenceman was on no current roster and the name matched the centre."""

from nhl_contracts.normalize import deadline_rosters as dr
from nhl_contracts.normalize.contracts import _espn_names_with_position, extract_espn
from nhl_contracts.normalize.identity import (
    FLAG_AMBIGUOUS,
    FLAG_HISTORY,
    FLAG_NAMESAKE,
    FLAG_ROSTER,
    FLAG_RULED_OUT,
    FLAG_SAME_CONTRACT,
    PlayerIndex,
    position_group,
)
from nhl_contracts.normalize.merge import assign_player_ids, group_claims, merge_contracts
from nhl_contracts.normalize.model import Claim, claim_id, make_key
from nhl_contracts.store import Store
from nhl_contracts.teams import fetch_player_seasons, registry_seasons

FWD, DEF = 8478427, 8480222
ROSTER = {FWD: ("Sebastian Aho", "CAR")}   # the defenceman is on no current list


def seasons(pid, name, pos, born, teams):
    return [{"player_id": pid, "season": s, "team": t, "full_name": name, "position": pos, "birth_date": born}
            for s, t in teams]


AHO_SEASONS = (
    seasons(FWD, "Sebastian Aho", "C", "1997-07-26",
            [(f"{y}-{str(y + 1)[2:]}", "CAR") for y in range(2016, 2027)])
    + seasons(DEF, "Sebastian Aho", "D", "1996-02-17",
              [("2017-18", "NYI"), ("2020-21", "NYI"), ("2021-22", "NYI"), ("2022-23", "NYI"), ("2023-24", "NYI")])
)


def index(extra=(), players=ROSTER):
    return PlayerIndex(players, [*AHO_SEASONS, *extra], current_season="2026-27")


def claim(name, team, signed_at, source, *, tier="C", pid=None, position=None, born=None, start=None, end=None,
          term=None, cap=None, flags=()):
    return Claim(
        claim_id=claim_id(source, name, pid), contract_key=make_key(pid, name, team, signed_at),
        player_name=name, player_id=pid, team=team, signed_at=signed_at, contract_type="standard",
        term_years=term, aav_usd=None, cap_hit_usd=cap, total_usd=None, nhl_salary_usd=None,
        minors_salary_usd=None, one_way=None, start_season=start, end_season=end, re_signed=False,
        source_id=source, source_url="u", publisher="p", published_at=signed_at, confidence_tier=tier,
        extracted_by="regex", flags=list(flags), position=position, birth_date=born,
        signing_date_stated=tier == "E",
    )


def aho_claims():
    """What the sources hold: ESPN's wire (position) and CapFriendly's two player pages
    (birth date), sebastian-aho (the centre) and sebastian-aho1 (the defenceman)."""
    return [
        claim("Sebastian Aho", "CAR", "2016-06-13", "cf:aho:0", tier="E", born="1997-07-26", term=3, cap=925_000,
              start="2016-17", end="2018-19"),
        claim("Sebastian Aho", "CAR", "2019-07-01", "cf:aho:1", tier="E", born="1997-07-26", term=5,
              cap=8_454_000, start="2019-20", end="2023-24"),
        claim("Sebastian Aho", "CAR", "2023-07-21", "espn:car23", position="RW", term=8),
        claim("Sebastian Aho", "CAR", "2023-07-26", "cf:aho:2", tier="E", born="1997-07-26", term=8,
              cap=9_750_000, start="2024-25", end="2031-32"),
        claim("Sebastian Aho", "NYI", "2017-07-05", "espn:nyi17", position="D", term=3),
        claim("Sebastian Aho", "NYI", "2017-07-05", "cf:aho1:0", tier="E", born="1996-02-17", term=3,
              cap=770_000, start="2017-18", end="2019-20"),
        claim("Sebastian Aho", "NYI", "2020-10-05", "espn:nyi20", position="D", term=2),
        claim("Sebastian Aho", "NYI", "2020-10-05", "cf:aho1:1", tier="E", born="1996-02-17", term=2,
              cap=725_000, start="2020-21", end="2021-22"),
        claim("Sebastian Aho", "NYI", "2022-07-09", "cf:aho1:2", tier="E", born="1996-02-17", term=2,
              cap=825_000, start="2022-23", end="2023-24"),
        claim("Sebastian Aho", "PIT", "2024-07-02", "espn:pit24", position="D", term=2),
        claim("Sebastian Aho", "PIT", "2024-07-02", "cf:aho1:3", tier="E", born="1996-02-17", term=2,
              cap=775_000, start="2024-25", end="2025-26"),
    ]


def test_two_sebastian_ahos_are_kept_apart():
    claims = aho_claims()
    assert assign_player_ids(claims, index()) == len(claims)
    by_team = {}
    for c in claims:
        by_team.setdefault(c.team, set()).add(c.player_id)
        assert FLAG_NAMESAKE in c.flags
    assert by_team == {"CAR": {FWD}, "NYI": {DEF}, "PIT": {DEF}}

    rows, _ = merge_contracts(claims)
    contracts = {(r["player_id"], r["team"], r["signed_at"][:7]) for r in rows}
    assert contracts == {
        (FWD, "CAR", "2016-06"), (FWD, "CAR", "2019-07"), (FWD, "CAR", "2023-07"),
        (DEF, "NYI", "2017-07"), (DEF, "NYI", "2020-10"), (DEF, "NYI", "2022-07"), (DEF, "PIT", "2024-07"),
    }
    pit = next(r for r in rows if r["team"] == "PIT")
    assert pit["contract_key"] == f"{DEF}|PIT|2024-07" and pit["cap_hit_usd"] == 775_000 and pit["n_sources"] == 2


def test_each_fact_alone_separates_the_ahos():
    idx = index()
    # club alone: the defenceman's Islanders seasons, the centre's Hurricanes seasons
    assert idx.resolve("Sebastian Aho", "NYI", "2020-10-05").player_id == DEF
    assert idx.resolve("Sebastian Aho", "CAR", "2023-07-21").player_id == FWD
    # birth date alone (no club)
    assert idx.resolve("Sebastian Aho", None, "2024-07-02", birth_date="1996-02-17").player_id == DEF
    # PIT 2024: the defenceman played no NHL game for Pittsburgh, so the club fits
    # neither; the position does, and rules the centre out
    pit = idx.resolve("Sebastian Aho", "PIT", "2024-07-02", position="D")
    assert (pit.player_id, pit.flag, pit.candidates) == (DEF, FLAG_NAMESAKE, (FWD, DEF))
    # nothing that tells them apart: named, not picked
    none = idx.resolve("Sebastian Aho", "PIT", "2024-07-02")
    assert (none.player_id, none.flag) == (None, FLAG_AMBIGUOUS)


def test_ambiguous_claim_takes_the_id_of_the_same_contract():
    espn = claim("Sebastian Aho", "PIT", "2024-07-02", "espn:pit24", term=2)          # no position written
    cf = claim("Sebastian Aho", "PIT", "2024-07-02", "cf:aho1:3", tier="E", born="1996-02-17", term=2,
               start="2024-25", end="2025-26")
    assign_player_ids([espn, cf], index())
    assert cf.player_id == DEF and espn.player_id == DEF
    assert FLAG_SAME_CONTRACT in espn.flags and FLAG_AMBIGUOUS not in espn.flags
    rows, _ = merge_contracts([espn, cf])
    assert len(rows) == 1 and rows[0]["player_id"] == DEF

    alone = claim("Sebastian Aho", "PIT", "2024-07-02", "espn:pit24", term=2)
    assign_player_ids([alone], index())
    assert alone.player_id is None and FLAG_AMBIGUOUS in alone.flags


def test_grouping_keeps_namesakes_apart():
    """Two ids, one name: each id is its own player, and a claim the index could not
    resolve joins neither."""
    a = claim("Sebastian Aho", "CAR", "2024-07-02", "x:1", pid=FWD)
    b = claim("Sebastian Aho", "PIT", "2024-07-02", "x:2", pid=DEF)
    c = claim("Sebastian Aho", "PIT", "2024-07-03", "x:3", flags=[FLAG_AMBIGUOUS])
    groups = group_claims([a, b, c])
    assert sorted(len(g) for g in groups) == [1, 1, 1]
    # a name only one id carries still joins it (the old behaviour)
    d = claim("Seth Jarvis", "CAR", "2024-07-02", "x:4", pid=8482093)
    e = claim("Seth Jarvis", "CAR", "2024-07-03", "x:5")
    assert [len(g) for g in group_claims([d, e])] == [2]


def test_one_known_player_and_the_facts_that_rule_him_out():
    idx = index([*seasons(8476473, "Connor Murphy", "D", "1993-03-26", [("2024-25", "CHI")])],
                players={**ROSTER, 8476473: ("Connor Murphy", "EDM")})
    # the club is not checked for a single candidate: sources disagree about clubs
    m = idx.resolve("Connor Murphy", "CGY", "2025-03-06")
    assert (m.player_id, m.flag) == (8476473, FLAG_ROSTER)
    # a goalie of the same name (ESPN: "Signed G Connor Murphy") is someone else
    m = idx.resolve("Connor Murphy", "CGY", "2025-03-06", position="G")
    assert (m.player_id, m.flag) == (None, FLAG_RULED_OUT)
    # forward versus defence alone is not enough (ESPN wrote "D Kirill Marchenko")
    assert idx.resolve("Connor Murphy", "CGY", "2025-03-06", position="F").player_id == 8476473
    # a birth date a few days off is a misprint, not another player; years off is
    assert idx.resolve("Connor Murphy", "CHI", "2021-08-31", birth_date="1993-03-21").player_id == 8476473
    assert idx.resolve("Connor Murphy", "CHI", "2021-08-31", birth_date="2003-05-01").player_id is None
    # no one signs at 12
    assert idx.resolve("Connor Murphy", "PHX", "2005-07-01").flag == FLAG_RULED_OUT


def test_alias_namesakes_and_history_flag():
    """The NHL lists the 1998 forward as "Nate Smith": "Nathan Smith" is both players."""
    idx = PlayerIndex({}, [
        *seasons(8468503, "Nathan Smith", "C", "1982-02-09", [("2008-09", "PIT"), ("2009-10", "MIN")]),
        *seasons(8480979, "Nate Smith", "C", "1998-10-19", [("2021-22", "ARI"), ("2022-23", "ARI")]),
    ], current_season="2026-27")
    m = idx.resolve("Nathan Smith", "UTA", "2023-07-01")          # ARI and UTA are one franchise here
    assert (m.player_id, m.flag) == (8480979, FLAG_NAMESAKE)
    old = idx.resolve("Nathan Smith", "PIT", "2008-07-01")
    assert (old.player_id, old.flag) == (8468503, FLAG_NAMESAKE)
    solo = PlayerIndex({}, seasons(8480979, "Nate Smith", "C", "1998-10-19", [("2022-23", "ARI")]))
    assert solo.resolve("Nate Smith", "ARI", "2022-07-01").flag == FLAG_HISTORY
    assert {n: ids for n, ids in idx.namesakes().items()} == {}    # exact names differ


def test_espn_positions():
    assert _espn_names_with_position("D Sebastian Aho and Grant Hutton") == [
        ("Sebastian Aho", "D"), ("Grant Hutton", None)]
    assert _espn_names_with_position("Fs Kieffer Bellows, Arnaud Durandeau and D Robin Salo") == [
        ("Kieffer Bellows", "Fs"), ("Arnaud Durandeau", "Fs"), ("Robin Salo", "D")]
    assert position_group("Fs") == "F" and position_group("LW/C") == "F" and position_group("RD") == "D"
    row = {"key": "k", "date": "2024-07-02T07:00Z", "team_abbrev": "PIT",
           "description": "Agreed to terms with D Sebastian Aho on a two-year contract."}
    claims, _ = extract_espn(row)
    assert [(c.player_name, c.position) for c in claims] == [("Sebastian Aho", "D")]


def test_deadline_rosters_use_birth_dates():
    rows = [{"player_name": "Sebastian Aho", "team": "NYI", "target": "2022-03-21", "as_of": "2022-03-01",
             "position": "LD", "birth_date": "1996-02-17", "player_id": None},
            {"player_name": "Sebastian Aho", "team": "CAR", "target": "2022-03-21", "as_of": "2022-03-01",
             "position": "C", "birth_date": "1997-07-26", "player_id": None}]
    assert dr.assign_ids(rows, index()) == 2
    assert [r["player_id"] for r in rows] == [DEF, FWD]


def test_fetch_player_seasons_and_store_roundtrip(tmp_path):
    class FakeClient:
        def __init__(self):
            self.urls = []

        def get_json(self, url, params=None):
            self.urls.append((url, params["cayenneExp"]))
            if url.endswith("skater/summary"):
                return {"data": [{"playerId": DEF, "skaterFullName": "Sebastian Aho", "positionCode": "D",
                                  "teamAbbrevs": "NYI"},
                                 {"playerId": 1, "skaterFullName": "Traded Guy", "positionCode": "C",
                                  "teamAbbrevs": "NYI,PIT"}]}
            if url.endswith("skater/bios"):
                return {"data": [{"playerId": DEF, "birthDate": "1996-02-17"}]}
            if url.endswith("goalie/summary"):
                return {"data": [{"playerId": 2, "goalieFullName": "A Goalie", "teamAbbrevs": "PIT"}]}
            return {"data": []}

    client = FakeClient()
    rows = fetch_player_seasons(client, ["2023-24"])
    assert ("https://api.nhle.com/stats/rest/en/skater/summary", "seasonId=20232024 and gameTypeId=2") in client.urls
    assert {(r["player_id"], r["team"], r["position"], r["birth_date"]) for r in rows} == {
        (DEF, "NYI", "D", "1996-02-17"), (1, "NYI", "C", None), (1, "PIT", "C", None), (2, "PIT", "G", None)}
    store = Store(tmp_path / "s.sqlite")
    assert store.set_player_seasons(rows, "2026-11-16") == 4
    assert store.player_season_seasons() == {"2023-24"}
    idx = PlayerIndex.from_store(store)
    assert idx.resolve("Sebastian Aho", "NYI", "2023-07-01").player_id == DEF
    assert registry_seasons()[0] == "2005-06"


def test_years_away_rule_out_namesakes_only():
    """Adam Clendening's last NHL game was in 2018-19; the Rangers' 2023 tryout is still
    him. Between namesakes, four seasons away does rule one out (Colin White, born 1977,
    is not the Devils' 2026 signing)."""
    clendening = PlayerIndex({}, seasons(8476952, "Adam Clendening", "D", "1992-10-26", [("2018-19", "CBJ")]))
    assert clendening.resolve("Adam Clendening", "NYR", "2023-09-17", position="D").player_id == 8476952
    whites = PlayerIndex({8478400: ("Colin White", "NJD")}, [
        *seasons(8465005, "Colin White", "D", "1977-12-12", [("2010-11", "NJD"), ("2011-12", "SJS")]),
        *seasons(8478400, "Colin White", "C", "1997-01-30", [("2025-26", "SJS")]),
    ], current_season="2026-27")
    m = whites.resolve("Colin White", "NJD", "2026-07-01")
    assert (m.player_id, m.flag) == (8478400, FLAG_NAMESAKE)


def test_dead_cap_rows_check_the_seasons_before_the_charge():
    """OTT's buyout of Colin White (born 1997) charges to 2027-28, years after his last
    game for Ottawa; the Colin White born 1977 left the league in 2012."""
    from nhl_contracts.normalize.model import DeadCapClaim

    whites = PlayerIndex({8478400: ("Colin White", "NJD")}, [
        *seasons(8465005, "Colin White", "D", "1977-12-12", [("2010-11", "NJD"), ("2011-12", "SJS")]),
        *seasons(8478400, "Colin White", "C", "1997-01-30", [("2019-20", "OTT"), ("2021-22", "OTT")]),
    ], current_season="2026-27")
    row = DeadCapClaim(claim_id="d", kind="buyout", player_name="Colin White", player_id=None, team="OTT",
                       counterparty=None, season="2027-28", cap_hit_usd=875_000, pct=None, contract_key=None,
                       event_date=None, source_id="wayback:x", confidence_tier="E")
    assert assign_player_ids([row], whites) == 1
    assert row.player_id == 8478400 and FLAG_NAMESAKE in row.flags
