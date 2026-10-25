# ruff: noqa: E501  -- real PuckPedia posts are quoted whole
"""Dead cap: retained salary and buyouts from PuckPedia posts (tier D) and the
archived CapFriendly team pages (tier E), their merge, and the export."""

import json
from datetime import date
from pathlib import Path

from conftest import FIXTURES

from nhl_contracts.normalize import capfriendly as cf
from nhl_contracts.normalize import dead_cap as dc
from nhl_contracts.publish.export import DEAD_CAP_COLUMNS, export

# Real PuckPedia posts (text as posted; dates are the posting dates).
FOWLER = ("🚨Trade\n\nTo #stlblues  \nCam Fowler, Yr 7/8 $6.5M ($2.5M retained)\n‘27 4th\n\nTo #Flytogether  \n"
          "‘27 2nd\nJeremie Biakabutuka, Yr 2/3 $825K\n\nNet cap change $4M per year for 2 years\n\npuckpedia.com/trade/908")
NYQUIST = ("🚨Trade\n\nTo #mnwild \nGustav Nyquist, $3.185M (50% retained)\n\nTo #Smashville\n'26 2nd\n\n"
           "MIN adds $1.593M annual cap hit\n\nhttps://puckpedia.com/trade/933?67c36518ea43c")
LINDGREN = ("🚨Trade\n\nTo #GoAvsGo\nRyan Lindgren, $4.5M (50%) retain\nHank Kempf, 22 y/o D, NCAA\nJimmy Vesey, $800K\n\n"
            "To #NYR\nParssinen, $775K\nDe Haan, $800K\n'25 2nd (Better of NYR or CAR Pick)\n'25 4th (Better of COL or VAN)\n\n"
            "COL Adds net $1.48M Cap Hit ($369K this yr)\n\nhttps://puckpedia.com/trade/932?67c35c18230b3")
CARLO = ("🚨 Trade\n\nTo #Stlblues\nBrandon Carlo 29 y/o D, Yr 6/6 $4.1M (15% retained by BOS) \n\nTo #LeafsForever\n"
         "'26 3rd (#73) - SJS\n'26 3rd (#76) - NJD\n\nSTL adds net $3.485M cap hit as BOS previously retained 15% on Carlo\n"
         "https://puckpedia.com/trade/1160?6a3ff40f1e493")
RANTANEN = ("#Blackhawks \nIn: '25 3rd, Retained $4.625M on Rantanen\nOut: Hall ($6M),  Juntorp\nNet: Saved $1.375M\n\n"
            "puckpedia.com/trades")
SMITH = ("🚨Trade\n\nTo #NYR \nBrendan Brisson, Yr 3/3 $925K\n'25 3rd (SJ)\n\nTo #VGK\nReilly Smith, Yr 3/3 @ $1.875M ($1.875M retained by NYR)\n\n"
         "VGK adds $1.875M cap hit\n\nhttps://puckpedia.com/trade/948?67c9f4b6429d6")
INGRAM = ("🚨Trade\n\nTo #LetsGoOilers\nConnor Ingram, 28 y/o G, Yr 3/3 $1.95M - $800K retained\n\nTo #TusksUp \nNothing\n\n"
          "EDM receives Ingram @ $1.15M, which is the burying threshold.\n\nhttps://puckpedia.com/trade/1043?68dd58ae")
LAUGHTON = ("🚨 Trade\n\nTo #GoKingsGo\nScott Laughton 31 y/o F, Yr 5/5 @ 3M ($1.5M retained previously by PHI)\n\nTo #LeafsForever \n"
            "- '26 3rd - Upgrades to a 2nd if LAK make playoffs\n\nLAK adds $1.5M net cap hit\n\nhttps://puckpedia.com/trade/1113?69ab4eee57ac6")
RANTANEN2 = ("🚨Trade\n\nTo #TexasHockey\nRantanen, $9.25M ($4.625M prev retained)\n\nTo #CauseChaos\nStankoven, Yr 2/3 $814K\n'26 1st\n\n"
             "https://puckpedia.com/trade/968?67cb6d985ca91")
VLASIC = ("The #SJSharks bought out the final 1 year @ $7M Cap Hit of 38 y/o LD Vlasic.\n\nBuyout Cap Hit:\n\n"
          "Year 1 $4.67M\nYear 2 $1.17M\n\nhttps://puckpedia.com/player/marc-edouard-vlasic/buyout?c=4120")
BRODIE = ("The #Blackhawks put TJ Brodie on waivers to buyout the remaining 1 year 3.75M of his contract. \n\n"
          "Buyout cap hit:\nYear 1 $3.23M\nYear 2 $258K\n\nhttps://puckpedia.com/player/tj-brodie/buyout?s=2025-2026")


def post(text, at, n=1):
    return {"uri": f"at://did:plc:x/app.bsky.feed.post/dead{n}", "created_at": at, "text": text, "links": []}


def rows_of(text, at):
    claims, source = dc.extract_post(post(text, at))
    assert source is not None, text[:40]
    return claims


def test_cap_season_turns_over_in_june():
    assert dc.cap_season(date(2025, 3, 7)) == "2024-25"
    assert dc.cap_season(date(2025, 6, 28)) == "2025-26"
    assert dc.cap_season(date(2025, 10, 1)) == "2025-26"


def test_stated_amount_and_years_left_give_the_whole_span():
    rows = rows_of(FOWLER, "2024-12-14T20:00:00Z")
    assert [(r.kind, r.team, r.counterparty, r.season, r.cap_hit_usd) for r in rows] == [
        ("retained", "ANA", "STL", "2024-25", 2_500_000), ("retained", "ANA", "STL", "2025-26", 2_500_000)]
    assert rows[0].pct == 38.5 and "derived:pct_from_amounts" in rows[0].flags
    assert "end_season_unknown" not in rows[0].flags
    assert rows[0].player_name == "Cam Fowler" and rows[0].event_date == "2024-12-14"


def test_percentage_of_the_line_cap_hit_without_a_span():
    (r,) = rows_of(NYQUIST, "2025-03-01T22:00:00Z")
    assert (r.team, r.counterparty, r.season, r.cap_hit_usd, r.pct) == ("NSH", "MIN", "2024-25", 1_592_500, 50.0)
    assert "derived:charge_from_pct" in r.flags and "end_season_unknown" in r.flags


def test_percent_in_parentheses_then_retain_word():
    (r,) = rows_of(LINDGREN, "2025-03-01T21:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.cap_hit_usd, r.pct) == ("Ryan Lindgren", "NYR", "COL", 2_250_000, 50.0)


def test_retained_by_a_third_team_is_charged_to_that_team():
    (r,) = rows_of(CARLO, "2026-03-05T18:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.season, r.cap_hit_usd, r.pct) == \
        ("Brandon Carlo", "BOS", "STL", "2025-26", 615_000, 15.0)
    assert "end_season_unknown" not in r.flags  # Yr 6/6: this season only


def test_retained_on_format_is_charged_to_the_posting_team():
    (r,) = rows_of(RANTANEN, "2025-03-07T21:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.season, r.cap_hit_usd) == ("Rantanen", "CHI", None, "2024-25", 4_625_000)
    assert {"retained_on_format", "end_season_unknown"} <= set(r.flags)


def test_stated_amount_with_by_and_amount_outside_parentheses():
    (r,) = rows_of(SMITH, "2025-03-06T20:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.cap_hit_usd, r.pct, r.season) == \
        ("Reilly Smith", "NYR", "VGK", 1_875_000, 100.0, "2024-25")
    (r,) = rows_of(INGRAM, "2025-10-01T18:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.cap_hit_usd, r.pct) == ("Connor Ingram", "UTA", "EDM", 800_000, 41.0)


def test_previously_retained_is_charged_to_the_named_team_or_left_unattributed():
    (r,) = rows_of(LAUGHTON, "2026-03-05T20:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.cap_hit_usd) == ("Scott Laughton", "PHI", "LAK", 1_500_000)
    assert "previous_retention" in r.flags and "retainer_unknown" not in r.flags
    (r,) = rows_of(RANTANEN2, "2025-03-07T23:00:00Z")
    assert (r.player_name, r.team, r.counterparty, r.cap_hit_usd) == ("Rantanen", None, "DAL", 4_625_000)
    assert {"previous_retention", "retainer_unknown"} <= set(r.flags)


def test_buyout_posts_give_one_row_per_charged_season_named_from_the_link():
    rows = rows_of(VLASIC, "2025-06-28T15:00:00Z")
    assert [(r.kind, r.team, r.player_name, r.season, r.cap_hit_usd) for r in rows] == [
        ("buyout", "SJS", "Marc Edouard Vlasic", "2025-26", 4_670_000), ("buyout", "SJS", "Marc Edouard Vlasic", "2026-27", 1_170_000)]
    assert "name_from_link" in rows[0].flags
    rows = rows_of(BRODIE, "2025-06-29T15:00:00Z")
    assert [(r.player_name, r.season, r.cap_hit_usd) for r in rows] == [("Tj Brodie", "2025-26", 3_230_000), ("Tj Brodie", "2026-27", 258_000)]


def test_signings_and_other_posts_yield_nothing():
    text = "The #NHLBruins signed 24 y/o F Marat Khusnutdinov to a three year $3.95M cap hit contract extension"
    assert dc.extract_post(post(text, "2026-07-01T12:00:00Z")) == ([], None)
    assert dc.extract_post(post("The first buyout window opens in 48 hours", "2025-06-20T12:00:00Z")) == ([], None)


def test_team_page_dead_cap_tables_buyouts_and_retained():
    oilers = cf.parse_team_dead_cap((FIXTURES / "wayback" / "teams-oilers-20240704221225.html").read_text(encoding="utf-8"),
                                    "https://web.archive.org/web/20240704221225/https://www.capfriendly.com/teams/oilers",
                                    "20240704221225")
    assert [(r["kind"], r["name"]) for r in oilers] == [("buyout", "James Neal"), ("buyout", "Jack Campbell")]
    habs = cf.parse_team_dead_cap((FIXTURES / "wayback" / "teams-canadiens-20240704224504.html").read_text(encoding="utf-8"),
                                  "https://web.archive.org/web/20240704224504/https://www.capfriendly.com/teams/canadiens",
                                  "20240704224504")
    retained = [r for r in habs if r["kind"] == "retained"]
    assert [(r["name"], r["team"]) for r in retained] == [("Jeff Petry", "MTL"), ("Jake Allen", "MTL")]
    assert retained[0]["seasons"] == [{"season": "2024-25", "cap_hit": 2_343_750, "aav": 2_343_750,
                                       "total_salary": 1_875_000, "signing_bonus": 750_000}]
    claims, sources = cf.dead_cap_claims(oilers + habs)
    campbell = [c for c in claims if c.player_name == "Jack Campbell"]
    assert [(c.season, c.cap_hit_usd) for c in campbell][:3] == [("2024-25", 1_100_000), ("2025-26", 2_300_000), ("2026-27", 2_600_000)]
    assert all(c.confidence_tier == "E" and c.kind == "buyout" and c.team == "EDM" for c in campbell)
    assert {c.kind for c in claims if c.team == "MTL"} == {"retained"}
    assert len(sources) == 2 and all(s.source_id.startswith("wayback:") for s in sources)


def test_extend_retentions_fills_the_span_from_the_contract_and_links_it():
    claims = rows_of(NYQUIST, "2025-03-01T22:00:00Z")
    contracts = [{"contract_key": "gustav nyquist|NSH|2023-07", "player_name": "Gustav Nyquist", "player_id": None,
                  "start_season": "2023-24", "end_season": "2025-26", "best_tier": "D"}]
    assert dc.extend_retentions(claims, contracts) == 1
    assert [(c.season, c.cap_hit_usd, c.contract_key) for c in claims] == [
        ("2024-25", 1_592_500, "gustav nyquist|NSH|2023-07"), ("2025-26", 1_592_500, "gustav nyquist|NSH|2023-07")]
    assert "derived:span_from_contract" in claims[1].flags
    # nothing to extend when no contract covers the season
    other = rows_of(RANTANEN, "2025-03-07T21:00:00Z")
    assert dc.extend_retentions(other, contracts) == 0


def test_merge_keeps_the_best_tier_and_names_disagreements():
    d_rows = rows_of(FOWLER, "2024-12-14T20:00:00Z")
    e_row = dc.DeadCapClaim(claim_id="e1", kind="retained", player_name="Cam Fowler", player_id=None, team="ANA",
                            counterparty=None, season="2024-25", cap_hit_usd=2_500_000, pct=None, contract_key=None,
                            event_date=None, source_id="wayback:1:/teams/ducks", confidence_tier="E",
                            flags=["team_page_dead_cap"])
    e_conflict = dc.DeadCapClaim(**{**e_row.__dict__, "claim_id": "e2", "season": "2025-26", "cap_hit_usd": 2_400_000})
    rows = dc.merge_dead_cap(d_rows + [e_row, e_conflict])
    assert [(r["season"], r["cap_hit_usd"], r["conflicts"], r["n_sources"], r["best_tier"]) for r in rows] == [
        ("2024-25", 2_500_000, "", 2, "D"), ("2025-26", 2_500_000, "cap_hit_usd", 2, "D")]
    assert rows[0]["counterparty"] == "STL" and rows[0]["pct"] == 38.5 and rows[0]["team"] == "ANA"


def test_export_writes_the_dead_cap_tables(tmp_path: Path):
    d_rows = rows_of(VLASIC, "2025-06-28T15:00:00Z")
    stamp = export([], [], [], tmp_path, dead_cap=dc.merge_dead_cap(d_rows), dead_cap_claims=d_rows)
    assert stamp["n_dead_cap"] == 2 and stamp["n_dead_cap_claims"] == 2
    assert {"dead_cap.parquet", "dead_cap.csv", "dead_cap.json", "dead_cap_claims.json"} <= set(stamp["files"])
    rows = json.loads((tmp_path / "dead_cap.json").read_text(encoding="utf-8"))
    assert list(rows[0].keys()) == DEAD_CAP_COLUMNS
    assert rows[0]["pct"] is None and rows[0]["cap_hit_usd"] == 4_670_000
