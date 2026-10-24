"""gaps.csv: roster players the dataset does not cover, with hints from the raw store."""

from datetime import date

from nhl_contracts.publish.gaps import GAP_COLUMNS, find_gaps, write_gaps
from nhl_contracts.store import Store


def test_gaps_classify_missing_money_and_structure(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    store.set_players({1: ("Sidney Crosby", "PIT", "roster"), 2: ("A.J. Greer", "ANA", "roster"),
                       3: ("Cutter Gauthier", "ANA", "roster"), 4: ("Full Coverage", "BOS", "roster"),
                       5: ("Some Prospect", "BOS", "prospect")}, "2026-10-24")
    store.upsert_espn([{"key": "k1", "date": "2026-06-30T07:00Z", "team_abbrev": "ANA", "team_id": "25",
                        "description": "Signed LW A.J. Greer to a four-year contract.", "raw": {}}], "2026-10-24")
    store.commit()
    contracts = [
        {"contract_key": "c2", "player_id": 2, "start_season": "2026-27", "end_season": "2029-30", "aav_usd": None,
         "cap_hit_usd": None},
        {"contract_key": "c3", "player_id": 3, "start_season": "2025-26", "end_season": "2032-33", "aav_usd": 7_850_000,
         "cap_hit_usd": None},
        {"contract_key": "c4", "player_id": 4, "start_season": "2024-25", "end_season": "2026-27", "aav_usd": 1_000_000,
         "cap_hit_usd": None},
        {"contract_key": "c1old", "player_id": 1, "start_season": "2024-25", "end_season": "2025-26",
         "aav_usd": 8_700_000, "cap_hit_usd": None},
    ]
    seasons = [{"contract_key": "c4", "season": "2026-27"}]
    gaps = find_gaps(store, contracts, seasons, today=date(2026, 10, 24))
    by = {g["player_name"]: g for g in gaps}
    assert set(by) == {"Sidney Crosby", "A.J. Greer", "Cutter Gauthier"}  # the prospect and the covered player are out
    assert by["Sidney Crosby"]["gap"] == "no_contract" and by["Sidney Crosby"]["status"] == "older_rows_only"
    assert by["Sidney Crosby"]["last_contract_end"] == "2025-26"
    assert by["A.J. Greer"]["gap"] == "no_money" and "four-year contract" in by["A.J. Greer"]["espn_hint"]
    assert by["Cutter Gauthier"]["gap"] == "no_structure"
    counts = write_gaps(gaps, tmp_path / "dist")
    assert counts == {"n_gaps": 3, "n_gaps_no_contract": 1, "n_gaps_no_money": 1, "n_gaps_no_structure": 1}
    header = (tmp_path / "dist" / "gaps.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(GAP_COLUMNS)
