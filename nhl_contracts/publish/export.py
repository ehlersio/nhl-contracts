"""Write the dataset files to ``dist/``: parquet, CSV and JSON for contracts and
claims, a CSV of sources, and ``timestamp.json`` (nflverse style)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .. import __version__
from ..normalize.model import Claim, ClauseClaim, DeadCapClaim, SeasonClaim, Source

CONTRACT_COLUMNS = [
    "contract_key", "player_name", "player_id", "team", "signed_at", "signing_date_stated", "contract_type",
    "term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd", "minors_salary_usd",
    "one_way", "start_season", "end_season", "end_season_signed", "bought_out", "expiry_status", "agent",
    "re_signed", "conflicts", "n_sources", "best_tier", "tiers", "source_ids", "claim_ids", "flags",
]
CLAIM_COLUMNS = [
    "claim_id", "contract_key", "player_name", "player_id", "team", "signed_at", "signing_date_stated",
    "contract_type", "term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd",
    "minors_salary_usd", "one_way", "start_season", "end_season", "expiry_status", "agent", "re_signed",
    "source_id", "source_url", "publisher", "published_at", "confidence_tier", "extracted_by", "flags",
    "evidence",
]
SEASON_COLUMNS = [
    "contract_key", "season", "cap_hit_usd", "aav_usd", "base_salary_usd", "signing_bonus_usd",
    "performance_bonus_usd", "total_salary_usd", "minors_salary_usd", "one_way", "clause", "conflicts",
    "n_sources", "best_tier", "source_ids",
]
SEASON_CLAIM_COLUMNS = [
    "claim_id", "contract_key", "season", "cap_hit_usd", "aav_usd", "base_salary_usd", "signing_bonus_usd",
    "performance_bonus_usd", "total_salary_usd", "minors_salary_usd", "one_way", "clause", "source_id",
    "confidence_tier", "evidence",
]
CLAUSE_COLUMNS = [
    "contract_key", "kind", "season_from", "season_to", "list_size", "list_type", "conflicts", "n_sources",
    "best_tier", "source_ids",
]
CLAUSE_CLAIM_COLUMNS = [
    "claim_id", "contract_key", "kind", "season_from", "season_to", "list_size", "list_type", "source_id",
    "confidence_tier", "evidence",
]
DEAD_CAP_COLUMNS = [
    "team", "kind", "player_name", "player_id", "counterparty", "season", "cap_hit_usd", "pct", "contract_key",
    "event_date", "conflicts", "n_sources", "best_tier", "source_ids", "flags",
]
DEAD_CAP_CLAIM_COLUMNS = [
    "claim_id", "team", "kind", "player_name", "player_id", "counterparty", "season", "cap_hit_usd", "pct",
    "contract_key", "event_date", "source_id", "confidence_tier", "flags", "evidence",
]
SOURCE_COLUMNS = ["source_id", "kind", "url", "publisher", "author", "published_at",
                  "confidence_tier", "headline"]

SCHEMA_TYPES = {
    "player_id": pa.int64(), "term_years": pa.int64(), "aav_usd": pa.int64(),
    "cap_hit_usd": pa.int64(), "total_usd": pa.int64(), "nhl_salary_usd": pa.int64(),
    "minors_salary_usd": pa.int64(), "one_way": pa.bool_(), "re_signed": pa.bool_(), "bought_out": pa.bool_(),
    "n_sources": pa.int64(), "signing_date_stated": pa.bool_(), "base_salary_usd": pa.int64(),
    "signing_bonus_usd": pa.int64(), "performance_bonus_usd": pa.int64(), "total_salary_usd": pa.int64(),
    "list_size": pa.int64(), "pct": pa.float64(),
}


def _rows(records: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [{c: r.get(c) for c in columns} for r in records]


def _write_parquet(rows: list[dict[str, Any]], columns: list[str], path: Path) -> None:
    fields = [pa.field(c, SCHEMA_TYPES.get(c, pa.string())) for c in columns]
    schema = pa.schema(fields)
    arrays = []
    for c in columns:
        typ = SCHEMA_TYPES.get(c, pa.string())
        vals = [r.get(c) for r in rows]
        if typ == pa.string():
            vals = [None if v is None else str(v) for v in vals]
        arrays.append(pa.array(vals, type=typ))
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), path, compression="zstd")


def _write_csv(rows: list[dict[str, Any]], columns: list[str], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def _write_json(rows: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")


def _write_set(rows: list[dict[str, Any]], columns: list[str], dist: Path, stem: str) -> list[str]:
    _write_parquet(rows, columns, dist / f"{stem}.parquet")
    _write_csv(rows, columns, dist / f"{stem}.csv")
    _write_json(rows, dist / f"{stem}.json")
    return [f"{stem}.parquet", f"{stem}.csv", f"{stem}.json"]


def export(contracts: list[dict[str, Any]], claims: list[Claim], sources: list[Source],
           dist: Path, *, seasons: list[dict[str, Any]] | None = None,
           season_claims: list[SeasonClaim] | None = None,
           clauses: list[dict[str, Any]] | None = None,
           clause_claims: list[ClauseClaim] | None = None,
           dead_cap: list[dict[str, Any]] | None = None,
           dead_cap_claims: list[DeadCapClaim] | None = None,
           store_counts: dict[str, int] | None = None) -> dict[str, Any]:
    dist.mkdir(parents=True, exist_ok=True)
    c_rows = _rows(contracts, CONTRACT_COLUMNS)
    cl_rows = _rows([c.to_dict() for c in claims], CLAIM_COLUMNS)
    se_rows = _rows(seasons or [], SEASON_COLUMNS)
    sc_rows = _rows([s.to_dict() for s in (season_claims or [])], SEASON_CLAIM_COLUMNS)
    cz_rows = _rows(clauses or [], CLAUSE_COLUMNS)
    cc_rows = _rows([c.to_dict() for c in (clause_claims or [])], CLAUSE_CLAIM_COLUMNS)
    dc_rows = _rows(dead_cap or [], DEAD_CAP_COLUMNS)
    dcc_rows = _rows([c.to_dict() for c in (dead_cap_claims or [])], DEAD_CAP_CLAIM_COLUMNS)
    seen: set[str] = set()
    s_rows = []
    for s in sources:
        if s.source_id in seen:
            continue
        seen.add(s.source_id)
        s_rows.append(asdict(s))
    s_rows = _rows(s_rows, SOURCE_COLUMNS)

    files = []
    files += _write_set(c_rows, CONTRACT_COLUMNS, dist, "contracts")
    files += _write_set(cl_rows, CLAIM_COLUMNS, dist, "claims")
    files += _write_set(se_rows, SEASON_COLUMNS, dist, "contract_seasons")
    files += _write_set(sc_rows, SEASON_CLAIM_COLUMNS, dist, "season_claims")
    files += _write_set(cz_rows, CLAUSE_COLUMNS, dist, "clauses")
    files += _write_set(cc_rows, CLAUSE_CLAIM_COLUMNS, dist, "clause_claims")
    files += _write_set(dc_rows, DEAD_CAP_COLUMNS, dist, "dead_cap")
    files += _write_set(dcc_rows, DEAD_CAP_CLAIM_COLUMNS, dist, "dead_cap_claims")
    _write_csv(s_rows, SOURCE_COLUMNS, dist / "sources.csv")
    files += ["sources.csv", "timestamp.json"]

    stamp = {
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "version": __version__,
        "n_contracts": len(c_rows),
        "n_claims": len(cl_rows),
        "n_sources": len(s_rows),
        "n_conflicts": sum(1 for r in c_rows if r["conflicts"]),
        "n_contract_seasons": len(se_rows),
        "n_season_claims": len(sc_rows),
        "n_clauses": len(cz_rows),
        "n_clause_claims": len(cc_rows),
        "n_dead_cap": len(dc_rows),
        "n_bought_out": sum(1 for r in c_rows if r["bought_out"]),
        "n_dead_cap_claims": len(dcc_rows),
        "tiers": {t: sum(1 for r in cl_rows if r["confidence_tier"] == t) for t in "ABCDE"},
        "earliest_signed_at": min((r["signed_at"] for r in c_rows), default=None),
        "latest_signed_at": max((r["signed_at"] for r in c_rows), default=None),
        "raw_store": store_counts or {},
        "files": files + ["gaps.csv", "gaps.json"],
        "license": {"data": "CC0-1.0", "code": "MIT"},
    }
    (dist / "timestamp.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return stamp
