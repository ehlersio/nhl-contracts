"""Command line entrypoint.

    python -m nhl_contracts.run all            # collect + normalize + export (hourly job)
    python -m nhl_contracts.run collect        # poll NHL.com, ESPN and PuckPedia's Bluesky feed
    python -m nhl_contracts.run backfill       # one-off history: per-player NHL.com lists, ESPN since
                                               # 2016, the whole Bluesky feed, CapFriendly snapshots
    python -m nhl_contracts.run normalize      # rebuild dist/ from the raw store (offline)
    python -m nhl_contracts.run load           # rebuild data/raw.sqlite from data/raw/*.jsonl + wayback_pages.sqlite
    python -m nhl_contracts.run dump           # write data/raw/*.jsonl (and the wayback file with --sources wayback)

The working store (data/raw.sqlite) is a cache. Its portable copies are the JSONL
files under data/raw/ (committed) and data/wayback_pages.sqlite (a release
asset). ``collect``, ``backfill`` and ``all`` load the copies into an empty store
first and dump the JSONL afterwards. Every step is idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .collectors import espn_transactions, nhl_forge
from .config import DIST_DIR, RAW_JSONL_DIR, STORE_PATH, WAYBACK_STORE
from .http import Client
from .normalize import llm_fallback
from .normalize.contracts import normalize_all
from .normalize.merge import assign_player_ids, merge_clauses, merge_contracts, merge_seasons
from .normalize.model import Claim, ClauseClaim, SeasonClaim, Source
from .publish.export import export
from .publish.gaps import find_gaps, write_gaps
from .store import Store
from .teams import load_teams

log = logging.getLogger("nhl_contracts")

SOURCES = ("forge", "espn", "bluesky", "reddit", "wayback")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nhl-contracts", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", nargs="?", default="all",
                   choices=["collect", "backfill", "normalize", "export", "all", "load", "dump"])
    p.add_argument("--store", default=str(STORE_PATH), help="SQLite raw store path")
    p.add_argument("--dist", default=str(DIST_DIR), help="output directory")
    p.add_argument("--raw-dir", default=str(RAW_JSONL_DIR), help="JSONL copies of the live-source tables")
    p.add_argument("--wayback-store", default=str(WAYBACK_STORE), help="SQLite file holding the archived pages")
    p.add_argument("--no-dump", action="store_true", help="do not refresh the JSONL copies after collecting")
    p.add_argument("--sources", default="forge,espn,bluesky,reddit",
                   help=f"comma list of sources to collect/backfill; any of {','.join(SOURCES)}")
    p.add_argument("--espn-days", type=int, default=45, help="days back to (re)fetch ESPN transactions")
    p.add_argument("--espn-from", type=date.fromisoformat, default=None,
                   help="explicit ESPN start date (YYYY-MM-DD), overrides --espn-days")
    p.add_argument("--bluesky-since", type=date.fromisoformat, default=None,
                   help="page the Bluesky feed back to this date (backfill default 2023-07-01)")
    p.add_argument("--max-stories", type=int, default=None, help="cap new Forge story fetches (testing)")
    p.add_argument("--max-players", type=int, default=None, help="cap players polled by backfill")
    p.add_argument("--max-pages", type=int, default=None, help="cap Wayback/Bluesky pages per run")
    p.add_argument("--wayback-pause", type=float, default=2.5, help="seconds between Wayback requests")
    p.add_argument("--refresh-players", action="store_true",
                   help="also refresh the roster/prospect player-id list during collect")
    p.add_argument("--no-llm", action="store_true", help="ignore OPENROUTER_API_KEY; regex only")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _sources(args: argparse.Namespace) -> set[str]:
    chosen = {s.strip() for s in args.sources.split(",") if s.strip()}
    unknown = chosen - set(SOURCES)
    if unknown:
        raise SystemExit(f"unknown sources: {sorted(unknown)}")
    return chosen


def do_load(store: Store, args: argparse.Namespace, *, only_if_empty: bool = False) -> None:
    counts = store.counts()
    if not only_if_empty or counts["forge_stories"] == 0:
        loaded = store.load_jsonl(args.raw_dir)
        if loaded:
            log.info("load: %s from %s", loaded, args.raw_dir)
    if not only_if_empty or counts["wayback_pages"] == 0:
        n = store.load_wayback(args.wayback_store)
        if n:
            log.info("load: %d archived pages from %s", n, args.wayback_store)


def do_dump(store: Store, args: argparse.Namespace, *, wayback: bool = False) -> None:
    counts = store.dump_jsonl(args.raw_dir)
    log.info("dump: %s -> %s", counts, args.raw_dir)
    if wayback:
        n = store.dump_wayback(args.wayback_store)
        log.info("dump: %d archived pages -> %s", n, args.wayback_store)


def do_collect(store: Store, args: argparse.Namespace) -> None:
    chosen = _sources(args)
    with Client() as client:
        teams = load_teams(store, client)
        if "forge" in chosen:
            nhl_forge.collect(store, client, sorted(teams), max_stories=args.max_stories)
            if args.refresh_players:
                from .teams import fetch_player_ids

                store.set_players(fetch_player_ids(client, teams), date.today().isoformat())
                store.commit()
        if "espn" in chosen:
            start = args.espn_from or (date.today() - timedelta(days=args.espn_days))
            espn_transactions.collect(store, client, start, date.today())
        if "bluesky" in chosen:
            from .collectors import bluesky_puckpedia

            bluesky_puckpedia.collect(store, client, since=args.bluesky_since, max_pages=args.max_pages)
        if "reddit" in chosen:
            _collect_reddit(store)
        if "wayback" in chosen:
            _collect_wayback(store, args)


def _collect_reddit(store: Store) -> None:
    """Reddit rate-limits unauthenticated readers hard; a 429 is skipped (the next hourly
    run reads the same feeds) instead of retried within seconds."""
    from .collectors import reddit_hockey

    with Client(retries=1, pause=2.0) as r_client:
        reddit_hockey.collect(store, r_client)


def _collect_wayback(store: Store, args: argparse.Namespace) -> None:
    from .collectors import wayback_capfriendly

    with Client(retries=1, pause=0) as wb_client:
        wayback_capfriendly.collect(store, wb_client, max_pages=args.max_pages, pause=args.wayback_pause,
                                    retry_statuses=())


def do_backfill(store: Store, args: argparse.Namespace) -> None:
    chosen = _sources(args)
    with Client() as client:
        teams = load_teams(store, client)
        if "forge" in chosen:
            from .teams import fetch_player_ids

            players = fetch_player_ids(client, teams)
            store.set_players(players, date.today().isoformat())
            store.commit()
            log.info("backfill: %d players on rosters and prospect lists", len(players))
            nhl_forge.backfill_players(store, client, sorted(players), max_players=args.max_players)
        if "espn" in chosen:
            start = args.espn_from or date(2016, 7, 1)
            espn_transactions.collect(store, client, start, date.today())
        if "bluesky" in chosen:
            from .collectors import bluesky_puckpedia

            since = args.bluesky_since or date(2023, 7, 1)
            bluesky_puckpedia.collect(store, client, since=since, max_pages=args.max_pages)
        if "reddit" in chosen:
            _collect_reddit(store)  # the feeds have no paging: this only reaches the last week
        if "wayback" in chosen:
            _collect_wayback(store, args)


def normalize_store(store: Store) -> dict[str, object]:
    teams = load_teams(store, None)
    claims: list[Claim] = []
    sources: list[Source] = []
    season_claims: list[SeasonClaim] = []
    clause_claims: list[ClauseClaim] = []

    stories = list(store.stories())
    espn_rows = list(store.espn_rows())
    f_claims, f_sources, _merged, f_seasons = normalize_all(stories, espn_rows, teams)
    claims += f_claims
    sources += f_sources
    season_claims += f_seasons
    log.info("normalize: %d stories, %d espn rows -> %d claims, %d season rows",
             len(stories), len(espn_rows), len(f_claims), len(f_seasons))

    posts = list(store.bsky_posts("puckpedia.com"))
    if posts:
        from .normalize import puckpedia

        b_claims, b_seasons, b_clauses, b_sources = puckpedia.normalize_posts(posts)
        claims += b_claims
        season_claims += b_seasons
        clause_claims += b_clauses
        sources += b_sources
        log.info("normalize: %d bluesky posts -> %d claims, %d season rows, %d clauses",
                 len(posts), len(b_claims), len(b_seasons), len(b_clauses))

    if store.counts().get("reddit_posts", 0):
        from .collectors import reddit_hockey
        from .normalize import reddit

        r_posts = reddit_hockey.posts(store)
        r_claims, r_seasons, r_clauses, r_sources = reddit.normalize_posts(r_posts)
        claims += r_claims
        season_claims += r_seasons
        clause_claims += r_clauses
        sources += r_sources
        log.info("normalize: %d r/hockey posts -> %d claims, %d season rows, %d clauses",
                 len(r_posts), len(r_claims), len(r_seasons), len(r_clauses))

    n_pages = store.counts().get("wayback_pages", 0)
    if n_pages:
        from .normalize import capfriendly

        w_claims, w_seasons, w_clauses, w_sources = capfriendly.rows_from_store(store)
        claims += w_claims
        season_claims += w_seasons
        clause_claims += w_clauses
        sources += w_sources
        log.info("normalize: %d archived CapFriendly pages -> %d claims, %d season rows, %d clauses",
                 n_pages, len(w_claims), len(w_seasons), len(w_clauses))

    filled = assign_player_ids(claims, store.players())
    if filled:
        log.info("normalize: %d claims given an NHL player id from the roster lists", filled)
    contracts, key_map = merge_contracts(claims)
    seasons = merge_seasons(season_claims, key_map)
    clauses = merge_clauses(clause_claims, key_map)
    if llm_fallback.wanted:
        log.info("llm fallback: %d stories eligible, %d answered%s", len(llm_fallback.wanted),
                 len(llm_fallback.used), "" if llm_fallback.is_enabled() else " (no extractor registered)")
    return {"claims": claims, "sources": sources, "contracts": contracts, "seasons": seasons,
            "season_claims": season_claims, "clauses": clauses, "clause_claims": clause_claims}


def do_export(store: Store, args: argparse.Namespace) -> None:
    n = normalize_store(store)
    stamp = export(n["contracts"], n["claims"], n["sources"], Path(args.dist),  # type: ignore[arg-type]
                   seasons=n["seasons"], season_claims=n["season_claims"],  # type: ignore[arg-type]
                   clauses=n["clauses"], clause_claims=n["clause_claims"],  # type: ignore[arg-type]
                   store_counts=store.counts())
    keys = ("n_contracts", "n_claims", "n_sources", "n_conflicts", "n_contract_seasons", "n_clauses")
    log.info("export: %s", json.dumps({k: stamp[k] for k in keys}))
    gaps = find_gaps(store, n["contracts"], n["seasons"])  # type: ignore[arg-type]
    counts = write_gaps(gaps, Path(args.dist))
    stamp.update(counts)
    (Path(args.dist) / "timestamp.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    log.info("gaps: %s", json.dumps(counts))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(args.store)
    extractor = None if args.no_llm else llm_fallback.configure_from_env(store=store)
    try:
        if args.step == "load":
            do_load(store, args)
        if args.step == "dump":
            do_dump(store, args, wayback="wayback" in _sources(args))
        if args.step in ("collect", "backfill", "all"):
            do_load(store, args, only_if_empty=True)
        if args.step == "collect" or args.step == "all":
            do_collect(store, args)
        if args.step == "backfill":
            do_backfill(store, args)
        if args.step in ("normalize", "export", "all", "backfill"):
            do_export(store, args)
        # after the export: normalizing fills the LLM cache, which must reach the JSONL copies
        if args.step in ("collect", "backfill", "all") and not args.no_dump:
            do_dump(store, args, wayback=(args.step == "backfill" and "wayback" in _sources(args)))
    finally:
        store.close()
        if extractor is not None and (extractor.calls or extractor.cache_hits):
            log.info("llm fallback: %d calls, %d cache hits, %d tokens in, %d out (%s)", extractor.calls,
                     extractor.cache_hits, extractor.tokens_in, extractor.tokens_out, extractor.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
