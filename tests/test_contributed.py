"""Hand-contributed claims (data/contributed/claims.csv)."""

import csv

from nhl_contracts.config import CONTRIBUTED_CLAIMS
from nhl_contracts.normalize.contributed import contributed_rows, read_claims
from nhl_contracts.normalize.model import claim_id, make_key
from nhl_contracts.publish.export import CLAIM_COLUMNS

ROW = {
    "claim_id": "", "contract_key": "8484999|WSH|2024-07", "player_name": "Ilya Protas", "player_id": "8484999",
    "team": "WSH", "signed_at": "2024-07-06", "signing_date_stated": "False", "contract_type": "ELC",
    "term_years": "3", "aav_usd": "889167", "cap_hit_usd": "", "total_usd": "", "nhl_salary_usd": "",
    "minors_salary_usd": "", "one_way": "", "start_season": "", "end_season": "", "expiry_status": "",
    "agent": "", "re_signed": "False", "source_id": "forge:capitals-sign-ilya-protas",
    "source_url": "https://www.nhl.com/news/capitals-sign-ilya-protas", "publisher": "WSH (club release on NHL.com)",
    "published_at": "2024-07-06T20:15:00Z", "confidence_tier": "A", "extracted_by": "regex",
    "flags": "multi_player_story", "evidence": "Protas' contract will carry an average annual value of $889,167.",
}
ROW["claim_id"] = claim_id(ROW["source_id"], ROW["player_name"], 8484999)


def _write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CLAIM_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_read_claims_types_fields(tmp_path):
    p = tmp_path / "claims.csv"
    _write(p, [ROW])
    (c,) = read_claims(p)
    assert c.player_id == 8484999 and c.term_years == 3 and c.aav_usd == 889_167 and c.total_usd is None
    assert c.one_way is None and c.re_signed is False and c.start_season is None and c.team == "WSH"
    assert c.flags == ["multi_player_story"] and c.extracted_by == "contributed"


def test_missing_file_yields_nothing(tmp_path):
    assert contributed_rows(tmp_path / "absent.csv", []) == ([], [])


def test_pipeline_claim_with_the_same_id_wins(tmp_path):
    p = tmp_path / "claims.csv"
    _write(p, [ROW])
    (parsed,) = read_claims(p)
    claims, sources = contributed_rows(p, [parsed])
    assert claims == [] and sources == []
    claims, sources = contributed_rows(p, [])
    assert len(claims) == 1 and [s.kind for s in sources] == ["contributed"]


def test_parsed_claim_without_an_id_yet_still_wins(tmp_path):
    # the parser hashed its claim_id from the name; the roster id is filled after this step
    p = tmp_path / "claims.csv"
    _write(p, [ROW])
    (parsed,) = read_claims(p)
    parsed.player_id = None
    parsed.claim_id = claim_id(parsed.source_id, parsed.player_name, None)
    assert contributed_rows(p, [parsed]) == ([], [])
    parsed.source_id = "forge:another-story"
    assert len(contributed_rows(p, [parsed])[0]) == 1


def test_committed_file_is_consistent():
    claims = read_claims(CONTRIBUTED_CLAIMS)
    assert claims, "data/contributed/claims.csv is empty or missing"
    with CONTRIBUTED_CLAIMS.open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == CLAIM_COLUMNS
    assert len({c.claim_id for c in claims}) == len(claims)
    for c in claims:
        # same id and key scheme as the normalizers, so a parsed claim replaces the contributed one
        sid = f"{c.source_id}#{c.signed_at[:7]}" if "describes_earlier_contract" in c.flags else c.source_id
        assert c.claim_id == claim_id(sid, c.player_name, c.player_id), c.player_name
        assert c.contract_key == make_key(c.player_id, c.player_name, c.team, c.signed_at), c.player_name
        assert c.source_url.startswith("https://") and c.evidence and c.confidence_tier in "ABCDE"


def test_row_about_an_earlier_contract_survives_the_story_s_own_claim(tmp_path):
    p = tmp_path / "claims.csv"
    earlier = dict(ROW, signed_at="2025-03-02", contract_key="8484999|WSH|2025-03",
                   flags="describes_earlier_contract")
    _write(p, [earlier])
    (row,) = read_claims(p)
    later = read_claims(p)[0]
    later.signed_at, later.contract_type, later.term_years = "2026-09-10", "standard", 6
    assert len(contributed_rows(p, [later])[0]) == 1  # the six-year deal does not replace the ELC row
    assert contributed_rows(p, [row]) == ([], [])     # a parsed claim for the same month does
