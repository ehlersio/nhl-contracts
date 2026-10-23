"""Command line entrypoint: ``python -m nhl_contracts.run [collect|normalize|export|all]``.

``all`` (the default) polls NHL.com and ESPN, normalizes the raw store and writes
``dist/``. Every step is idempotent, so re-running only adds what is new.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from .collectors import espn_transactions, nhl_forge
from .config import DIST_DIR, STORE_PATH
from .http import Client
from .normalize import llm_fallback
from .normalize.contracts import normalize_all
from .publish.export import export
from .store import Store
from .teams import load_teams

log = logging.getLogger("nhl_contracts")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nhl-contracts", description=__doc__)
    p.add_argument("step", nargs="?", default="all", choices=["collect", "normalize", "export", "all"])
    p.add_argument("--store", default=str(STORE_PATH), help="SQLite raw store path")
    p.add_argument("--dist", default=str(DIST_DIR), help="output directory")
    p.add_argument("--espn-days", type=int, default=45,
                   help="how many days back to (re)fetch ESPN transactions")
    p.add_argument("--espn-from", type=date.fromisoformat, default=None,
                   help="explicit ESPN start date (YYYY-MM-DD), overrides --espn-days")
    p.add_argument("--no-forge", action="store_true")
    p.add_argument("--no-espn", action="store_true")
    p.add_argument("--max-stories", type=int, default=None, help="cap new story fetches (testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(args.store)
    try:
        if args.step in ("collect", "all"):
            with Client() as client:
                teams = load_teams(store, client)
                if not args.no_forge:
                    nhl_forge.collect(store, client, sorted(teams), max_stories=args.max_stories)
                if not args.no_espn:
                    start = args.espn_from or (date.today() - timedelta(days=args.espn_days))
                    espn_transactions.collect(store, client, start, date.today())
        if args.step in ("normalize", "export", "all"):
            teams = load_teams(store, None)
            stories = list(store.stories())
            espn_rows = list(store.espn_rows())
            claims, sources, merged = normalize_all(stories, espn_rows, teams)
            log.info("normalize: %d stories, %d espn rows -> %d claims, %d contracts",
                     len(stories), len(espn_rows), len(claims), len(merged))
            if llm_fallback.wanted:
                log.info("llm fallback wanted for %d stories (no extractor registered)",
                         len(llm_fallback.wanted))
            from pathlib import Path

            stamp = export(merged, claims, sources, Path(args.dist), store_counts=store.counts())
            keys = ("n_contracts", "n_claims", "n_sources", "n_conflicts")
            log.info("export: %s", json.dumps({k: stamp[k] for k in keys}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
