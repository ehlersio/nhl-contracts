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
from ..normalize.contracts import Claim, Source

CONTRACT_COLUMNS = [
    "contract_key", "player_name", "player_id", "team", "signed_at", "contract_type",
    "term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd", "minors_salary_usd",
    "one_way", "start_season", "end_season", "re_signed", "conflicts", "n_sources", "best_tier",
    "source_ids", "claim_ids", "flags",
]
CLAIM_COLUMNS = [
    "claim_id", "contract_key", "player_name", "player_id", "team", "signed_at", "contract_type",
    "term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd", "minors_salary_usd",
    "one_way", "start_season", "end_season", "re_signed", "source_id", "source_url", "publisher",
    "published_at", "confidence_tier", "extracted_by", "flags", "evidence",
]
SOURCE_COLUMNS = ["source_id", "kind", "url", "publisher", "author", "published_at",
                  "confidence_tier", "headline"]

SCHEMA_TYPES = {
    "player_id": pa.int64(), "term_years": pa.int64(), "aav_usd": pa.int64(),
    "cap_hit_usd": pa.int64(), "total_usd": pa.int64(), "nhl_salary_usd": pa.int64(),
    "minors_salary_usd": pa.int64(), "one_way": pa.bool_(), "re_signed": pa.bool_(),
    "n_sources": pa.int64(),
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


def export(contracts: list[dict[str, Any]], claims: list[Claim], sources: list[Source],
           dist: Path, *, store_counts: dict[str, int] | None = None) -> dict[str, Any]:
    dist.mkdir(parents=True, exist_ok=True)
    c_rows = _rows(contracts, CONTRACT_COLUMNS)
    cl_rows = _rows([c.to_dict() for c in claims], CLAIM_COLUMNS)
    seen: set[str] = set()
    s_rows = []
    for s in sources:
        if s.source_id in seen:
            continue
        seen.add(s.source_id)
        s_rows.append(asdict(s))
    s_rows = _rows(s_rows, SOURCE_COLUMNS)

    _write_parquet(c_rows, CONTRACT_COLUMNS, dist / "contracts.parquet")
    _write_csv(c_rows, CONTRACT_COLUMNS, dist / "contracts.csv")
    _write_json(c_rows, dist / "contracts.json")
    _write_parquet(cl_rows, CLAIM_COLUMNS, dist / "claims.parquet")
    _write_csv(cl_rows, CLAIM_COLUMNS, dist / "claims.csv")
    _write_json(cl_rows, dist / "claims.json")
    _write_csv(s_rows, SOURCE_COLUMNS, dist / "sources.csv")

    stamp = {
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "version": __version__,
        "n_contracts": len(c_rows),
        "n_claims": len(cl_rows),
        "n_sources": len(s_rows),
        "n_conflicts": sum(1 for r in c_rows if r["conflicts"]),
        "earliest_signed_at": min((r["signed_at"] for r in c_rows), default=None),
        "latest_signed_at": max((r["signed_at"] for r in c_rows), default=None),
        "raw_store": store_counts or {},
        "files": ["contracts.parquet", "contracts.csv", "contracts.json", "claims.parquet",
                  "claims.csv", "claims.json", "sources.csv", "timestamp.json"],
        "license": {"data": "CC0-1.0", "code": "MIT"},
    }
    (dist / "timestamp.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return stamp
