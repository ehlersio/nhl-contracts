"""A buyout ends the contract it buys out (``normalize.buyouts.apply_buyouts``)."""

import json
from pathlib import Path

import pyarrow.parquet as pq

from nhl_contracts.normalize import puckpedia as pp
from nhl_contracts.normalize.buyouts import apply_buyouts
from nhl_contracts.normalize.text import shift_season
from nhl_contracts.publish.export import CONTRACT_COLUMNS, export


def contract(key, name, team, start, end, *, pid=None, tier="C", flags=""):
    return {"contract_key": key, "player_name": name, "player_id": pid, "team": team, "start_season": start,
            "end_season": end, "best_tier": tier, "flags": flags, "signed_at": "2019-06-07", "conflicts": ""}


def charge(name, team, season, usd, *, kind="buyout", pid=None, tier="E", key=None):
    return {"team": team, "kind": kind, "player_name": name, "player_id": pid, "counterparty": None,
            "season": season, "cap_hit_usd": usd, "pct": None, "contract_key": key, "event_date": None,
            "conflicts": "", "n_sources": 1, "best_tier": tier, "source_ids": "wayback:1:/teams/sabres",
            "flags": "team_page_dead_cap"}


def seasons_of(key, start, end):
    out, sn = [], start
    while sn <= end:
        out.append({"contract_key": key, "season": sn, "cap_hit_usd": 9_000_000})
        sn = shift_season(sn, 1)
    return out


SKINNER = contract("jeff skinner|BUF|2019-06", "Jeff Skinner", "BUF", "2019-20", "2026-27", pid=8475784)
SKINNER_SEASONS = seasons_of(SKINNER["contract_key"], "2019-20", "2026-27")
SKINNER_BUYOUT = [charge("Jeff Skinner", "BUF", sn, usd, pid=8475784, tier="D")
                  for sn, usd in (("2024-25", 1_444_445), ("2025-26", 4_444_445), ("2026-27", 6_444_445),
                                  ("2027-28", 2_444_445))]
OTHER = contract("8478400|OTT|2019-08", "Colin White", "OTT", "2019-20", "2024-25", pid=8478400)


def test_a_buyout_ends_the_contract_the_season_before_its_first_charge():
    out = apply_buyouts([SKINNER, OTHER], SKINNER_SEASONS, SKINNER_BUYOUT)
    sk = out["contracts"][0]
    assert sk["end_season"] == "2023-24" and sk["end_season_signed"] == "2026-27" and sk["bought_out"] is True
    assert {"bought_out", "derived:end_season_from_buyout"} <= set(sk["flags"].split(";"))
    assert "bought_out_team_mismatch" not in sk["flags"]
    assert [s["season"] for s in out["seasons"]] == ["2019-20", "2020-21", "2021-22", "2022-23", "2023-24"]
    assert out["n_bought_out"] == 1 and out["n_seasons_dropped"] == 3
    assert {d["contract_key"] for d in out["dead_cap"]} == {"jeff skinner|BUF|2019-06"}
    # the untouched contract still has the uniform columns
    assert out["contracts"][1]["bought_out"] is False and out["contracts"][1]["end_season_signed"] is None
    assert out["contracts"][1]["end_season"] == "2024-25"
    # pure: the inputs are not modified
    assert SKINNER["end_season"] == "2026-27" and "bought_out" not in SKINNER
    assert len(SKINNER_SEASONS) == 8 and SKINNER_BUYOUT[0]["contract_key"] is None


def test_charges_seen_only_on_the_archived_pages_are_an_upper_bound():
    archived = [{**d, "best_tier": "E"} for d in SKINNER_BUYOUT]
    sk = apply_buyouts([SKINNER], SKINNER_SEASONS, archived)["contracts"][0]
    assert sk["end_season"] == "2023-24" and "buyout_season_upper_bound" in sk["flags"]
    stated = apply_buyouts([SKINNER], SKINNER_SEASONS, SKINNER_BUYOUT)["contracts"][0]
    assert "buyout_season_upper_bound" not in stated["flags"]


def test_the_id_keyed_and_the_name_keyed_rows_of_one_deal_are_both_ended():
    by_name = contract("jeff skinner|BUF|2019-07", "Jeff Skinner", "BUF", "2019-20", "2026-27", tier="E")
    seasons = SKINNER_SEASONS + seasons_of(by_name["contract_key"], "2019-20", "2026-27")
    out = apply_buyouts([SKINNER, by_name], seasons, SKINNER_BUYOUT)
    assert [(r["end_season"], r["bought_out"]) for r in out["contracts"]] == [("2023-24", True), ("2023-24", True)]
    assert out["n_bought_out"] == 2 and out["n_seasons_dropped"] == 6
    # the dead-cap rows link the best-tier row
    assert {d["contract_key"] for d in out["dead_cap"]} == {"jeff skinner|BUF|2019-06"}


def test_a_name_only_buyout_matches_hyphenated_names_and_another_teams_deal():
    vlasic = contract("marcedouard vlasic|SJS|2017-07", "Marc-Édouard Vlasic", "SJS", "2018-19", "2025-26")
    rows = [charge("Marc Edouard Vlasic", "SJS", "2025-26", 4_670_000, tier="D"),
            charge("Marc Edouard Vlasic", "SJS", "2026-27", 1_170_000, tier="D")]
    assert apply_buyouts([vlasic], [], rows)["contracts"][0]["end_season"] == "2024-25"
    # a contract signed with one team, bought out by the team it was traded to
    schmidt = contract("8477220|VGK|2018-10", "Nate Schmidt", "VGK", "2019-20", "2024-25", pid=8477220)
    nxt = contract("8477220|FLA|2024-07", "Nate Schmidt", "FLA", "2024-25", "2024-25", pid=8477220)
    out = apply_buyouts([schmidt, nxt], [], [charge("Nate Schmidt", "WPG", "2024-25", 2_966_667, pid=8477220)])
    old, new = out["contracts"]
    assert old["end_season"] == "2023-24" and "bought_out_team_mismatch" in old["flags"]
    # the deal he signed elsewhere after the buyout is not touched
    assert new["bought_out"] is False and new["end_season"] == "2024-25"
    assert out["dead_cap"][0]["contract_key"] == "8477220|VGK|2018-10"


def test_a_buyout_before_the_deal_started_keeps_its_end():
    deal = contract("x|SJS|2025-06", "Some Player", "SJS", "2025-26", "2026-27")
    out = apply_buyouts([deal], seasons_of(deal["contract_key"], "2025-26", "2026-27"),
                        [charge("Some Player", "SJS", "2025-26", 1_000_000)])
    r = out["contracts"][0]
    assert r["end_season"] == "2026-27" and r["bought_out"] is True
    assert "bought_out_before_start" in r["flags"] and "derived:end_season_from_buyout" not in r["flags"]
    assert out["n_seasons_dropped"] == 0


def test_no_matching_contract_and_retentions_change_nothing():
    retained = [charge("Jeff Skinner", "BUF", "2024-25", 4_500_000, kind="retained", pid=8475784)]
    stranger = [charge("Nobody Known", "BUF", "2024-25", 1_000_000)]
    later = [charge("Jeff Skinner", "BUF", "2027-28", 1_000_000, pid=8475784)]  # after the deal ended
    for rows in (retained, stranger, later, []):
        out = apply_buyouts([SKINNER], SKINNER_SEASONS, rows)
        assert out["contracts"][0]["end_season"] == "2026-27" and out["contracts"][0]["bought_out"] is False
        assert out["n_bought_out"] == 0 and len(out["seasons"]) == 8
        assert all(d["contract_key"] is None for d in out["dead_cap"])


def test_a_buyout_post_is_not_read_as_a_signing():
    text = ("The #SJSharks bought out the final 1 year @ $7M Cap Hit of 38 y/o LD Vlasic.\n\nBuyout Cap Hit:\n\n"
            "Year 1 $4.67M\nYear 2 $1.17M\n\nhttps://puckpedia.com/player/marc-edouard-vlasic/buyout?c=4120")
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/vlasic", "created_at": "2025-06-26T15:00:00Z", "text": text,
            "links": []}
    assert pp.classify_post(text) == "buyout"
    assert pp.extract_post(post) == ([], [], [], None)


def test_export_writes_the_buyout_columns(tmp_path: Path):
    out = apply_buyouts([SKINNER, OTHER], SKINNER_SEASONS, SKINNER_BUYOUT)
    stamp = export(out["contracts"], [], [], tmp_path, seasons=out["seasons"], dead_cap=out["dead_cap"])
    assert stamp["n_bought_out"] == 1 and stamp["n_contract_seasons"] == 5
    rows = json.loads((tmp_path / "contracts.json").read_text(encoding="utf-8"))
    assert list(rows[0].keys()) == CONTRACT_COLUMNS
    assert [(r["bought_out"], r["end_season_signed"]) for r in rows] == [(True, "2026-27"), (False, None)]
    table = pq.read_table(tmp_path / "contracts.parquet")
    assert table.schema.field("bought_out").type == "bool"
    assert json.loads((tmp_path / "timestamp.json").read_text())["n_bought_out"] == 1
