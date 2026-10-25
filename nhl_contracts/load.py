"""Thin loader: download the published dataset from the GitHub release.

    import nhl_contracts
    contracts = nhl_contracts.load("contracts")          # list of dicts (JSON)
    table = nhl_contracts.load("contract_seasons", fmt="parquet")   # pyarrow.Table
    nhl_contracts.timestamp()                             # {"updated_at": ..., counts ...}

Files are cached under ``~/.cache/nhl-contracts`` (override with
``NHL_CONTRACTS_CACHE``) and re-downloaded when the release's ``timestamp.json``
is newer than the cached copy.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

REPO = os.environ.get("NHL_CONTRACTS_REPO", "ehlersio/nhl-contracts")
TAG = os.environ.get("NHL_CONTRACTS_TAG", "contracts")
BASE_URL = f"https://github.com/{REPO}/releases/download/{TAG}"
TABLES = ("contracts", "claims", "contract_seasons", "season_claims", "clauses", "clause_claims", "dead_cap",
          "dead_cap_claims")
FORMATS = ("json", "csv", "parquet")


def cache_dir() -> Path:
    d = Path(os.environ.get("NHL_CONTRACTS_CACHE", Path.home() / ".cache" / "nhl-contracts"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_url(name: str) -> str:
    return f"{BASE_URL}/{name}"


def download(name: str, *, max_age: float = 3600.0, force: bool = False) -> Path:
    """Fetch one release file into the cache (re-used for ``max_age`` seconds)."""
    target = cache_dir() / name
    if not force and target.exists() and time.time() - target.stat().st_mtime < max_age:
        return target
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        resp = client.get(file_url(name))
        resp.raise_for_status()
        target.write_bytes(resp.content)
    return target


def timestamp(**kw: Any) -> dict[str, Any]:
    return json.loads(download("timestamp.json", **kw).read_text(encoding="utf-8"))


def load(table: str = "contracts", fmt: str = "json", **kw: Any) -> Any:
    """Return a table from the latest release.

    ``fmt="json"`` gives a list of dicts, ``"csv"`` a list of dicts with string values,
    ``"parquet"`` a ``pyarrow.Table`` (pyarrow required). ``sources`` is CSV only.
    """
    if table == "sources":
        fmt = "csv"
    elif table not in TABLES:
        raise ValueError(f"unknown table {table!r}; one of {TABLES + ('sources',)}")
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; one of {FORMATS}")
    path = download(f"{table}.{fmt}", **kw)
    if fmt == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    if fmt == "csv":
        import csv

        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    import pyarrow.parquet as pq

    return pq.read_table(path)
