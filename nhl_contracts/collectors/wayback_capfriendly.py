"""Collect archived CapFriendly team and player pages from the Internet Archive.

CapFriendly shut down in July 2024; only Wayback snapshots are read, never the
live domain. For each page one CDX query lists the 2024 captures (collapsed to
one per day), the collector picks a snapshot and fetches its original bytes with
the ``id_`` flag, then stores the page gzip-compressed in ``wayback_pages``.

Snapshot choice, latest first within each tier:
1. 2024-06-25 .. 2024-07-09 with CDX length above 10000 (the final pre-shutdown pages)
2. 2024-04-01 .. 2024-07-09
3. any other 2024 capture (before April, then after the shutdown)
Within tiers 2 and 3 captures above 10000 bytes are tried first. A page is valid
when it carries its marker ("Years Remaining" for teams, "Signing Date" for
players). The post-shutdown "Thank You" page (about 8 KB, CDX length ~3.5 KB) is
kept with status ``closing-page`` when nothing valid exists.

The CDX service refuses connections after about a dozen rapid queries, so every
Wayback request is spaced ``pause`` seconds apart and connection errors, 429 and
5xx back off 10 s, 30 s, 90 s. A full run (33 team pages, ~1,600 player pages,
two requests each) takes well over an hour; it is resumable, and ``max_pages``
caps one run. Pass a ``Client(retries=1)`` so the client's own quick retries do
not add bursts on top of this pacing.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..http import Client
from ..normalize.capfriendly import CF_BASE, TEAM_SLUGS, snapshot_ts_of, snapshot_url_for, team_page_player_paths
from ..store import Store

log = logging.getLogger(__name__)

CDX = "https://web.archive.org/cdx/search/cdx"
# Date windows for the bulk index: narrow enough for the CDX service to answer (a
# whole-year wildcard query times out), together covering the last months of the site.
BULK_WINDOWS = (("20240101", "20240331"), ("20240401", "20240531"), ("20240601", "20240619"),
                ("20240620", "20240709"))
SNAPSHOT = "https://web.archive.org/web/{ts}id_/{url}"
MARKERS = {"team": "Years Remaining", "player": "Signing Date"}
FINAL_WINDOW = ("20240625000000", "20240709235959")
SPRING_WINDOW = ("20240401000000", "20240709235959")
MIN_LENGTH = 10_000
CLOSING_MAX_BYTES = 12_000
DONE_STATUSES = ("ok", "closing-page", "missing")
PROGRESS_EVERY = 25


class WaybackUnavailable(RuntimeError):
    """The archive kept refusing or failing after every back-off step."""


def page_url(path: str) -> str:
    return f"{CF_BASE}{path}"


def team_urls(slugs: Iterable[str] | None = None) -> list[str]:
    return [page_url(f"/teams/{s}") for s in (slugs or TEAM_SLUGS)]


def classify(html: str, kind: str) -> str:
    """ok | closing-page | error for fetched snapshot HTML."""
    if MARKERS.get(kind, "\0") in html:
        return "ok"
    if "<title>Thank You - CapFriendly" in html or len(html.encode("utf-8")) < CLOSING_MAX_BYTES:
        return "closing-page"
    return "error"


def parse_cdx(payload: Any) -> list[tuple[str, str, int | None]]:
    """CDX JSON (header row first) -> [(timestamp, original, length)]."""
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    try:
        i_ts, i_orig, i_len = header.index("timestamp"), header.index("original"), header.index("length")
    except ValueError:
        return []
    out = []
    for row in payload[1:]:
        length = row[i_len]
        out.append((row[i_ts], row[i_orig], int(length) if str(length).isdigit() else None))
    return out


def rank_snapshots(rows: list[tuple[str, str, int | None]]) -> list[tuple[str, str, int | None]]:
    """Order CDX rows by preference (see module docstring)."""

    def key(r: tuple[str, str, int | None]) -> tuple[int, int, int]:
        ts, _orig, length = r
        big = length is not None and length > MIN_LENGTH
        if FINAL_WINDOW[0] <= ts <= FINAL_WINDOW[1] and big:
            tier = 0
        elif SPRING_WINDOW[0] <= ts <= SPRING_WINDOW[1]:
            tier = 1
        elif ts < SPRING_WINDOW[0]:
            tier = 2
        else:
            tier = 3
        return tier, 0 if big else 1, -int(ts[:14].ljust(14, "0"))

    return sorted((r for r in rows if r[0].startswith("2024")), key=key)


class Wayback:
    """Paced access to the CDX index and snapshot bytes through a ``Client``."""

    def __init__(self, client: Client, *, pause: float = 2.5, backoff: tuple[float, ...] = (10, 30, 90),
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.pause = pause
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self.requests = 0
        self._last: float | None = None

    def _space(self) -> None:
        if self.pause and self._last is not None:
            wait = self.pause - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def get(self, url: str, params: dict[str, Any] | None = None) -> tuple[int, bytes, str]:
        last: Exception | None = None
        for attempt in range(len(self.backoff) + 1):
            self._space()
            self.requests += 1
            try:
                status, body, final = self.client.get_bytes(url, params, ok_statuses=(200, 404))
            except RuntimeError as exc:  # the client gives up on transport errors, 429 and 5xx
                last = exc
            else:
                if status != 429 and status < 500:
                    return status, body, final
                last = RuntimeError(f"HTTP {status} from {url}")
            if attempt < len(self.backoff):
                log.warning("wayback %s failed (%s); backing off %.0fs", url, last, self.backoff[attempt])
                self.sleep(self.backoff[attempt])
                self._last = self.clock()
        raise WaybackUnavailable(f"{url} failed after {len(self.backoff) + 1} attempts") from last

    def captures(self, url: str, start: str = "2024", end: str = "2024") -> list[tuple[str, str, int | None]]:
        bare = url.split("://", 1)[-1].removeprefix("www.")
        params = {"url": bare, "output": "json", "from": start, "to": end, "filter": "statuscode:200",
                  "fl": "timestamp,original,length", "collapse": "timestamp:8"}
        status, body, _final = self.get(CDX, params)
        if status != 200 or not body.strip():
            return []
        try:
            return parse_cdx(json.loads(body))
        except ValueError:
            log.warning("wayback CDX for %s returned non-JSON", url)
            return []

    def bulk_captures(self, prefix: str = "capfriendly.com/players/*", windows: Iterable[tuple[str, str]] = BULK_WINDOWS
                      ) -> dict[str, list[tuple[str, str, int | None]]]:
        """One CDX query per date window for every page under ``prefix`` (collapsed to one
        capture per page per window). Four queries replace about 1,600 per-page ones."""
        index: dict[str, list[tuple[str, str, int | None]]] = {}
        for start, end in windows:
            params = {"url": prefix, "output": "json", "from": start, "to": end, "filter": "statuscode:200",
                      "fl": "timestamp,original,length", "collapse": "urlkey"}
            status, body, _final = self.get(CDX, params)
            if status != 200 or not body.strip():
                log.warning("wayback bulk CDX %s-%s returned HTTP %s", start, end, status)
                continue
            try:
                rows = parse_cdx(json.loads(body))
            except ValueError:
                log.warning("wayback bulk CDX %s-%s returned non-JSON", start, end)
                continue
            for row in rows:
                index.setdefault(_bare(row[1]), []).append(row)
            log.info("wayback bulk CDX %s-%s: %d pages", start, end, len(rows))
        return index

    def snapshot(self, ts: str, original: str) -> tuple[int, str, str]:
        """(status, html, timestamp actually served: Wayback may redirect to a neighbouring capture)."""
        status, body, final = self.get(SNAPSHOT.format(ts=ts, url=original))
        return status, body.decode("utf-8", "replace"), snapshot_ts_of(final) or ts


def _bare(url: str) -> str:
    return url.split("://", 1)[-1].removeprefix("www.").rstrip("/")


def fetch_page(wb: Wayback, url: str, kind: str, *, max_attempts: int = 3,
               index: dict[str, list[tuple[str, str, int | None]]] | None = None) -> dict[str, Any]:
    """Choose and fetch the best snapshot of one page. Returns the row to store. With a bulk
    ``index`` the per-page CDX query is skipped when the page is in it."""
    path = url.removeprefix(CF_BASE)
    rows = index.get(_bare(url)) if index is not None else None
    ranked = rank_snapshots(rows if rows else wb.captures(url))
    if not ranked:
        return {"status": "missing", "snapshot_ts": "", "snapshot_url": "", "html": ""}
    fallback: dict[str, Any] | None = None
    for ts, original, _length in ranked[:max_attempts]:
        status, html, served = wb.snapshot(ts, original)
        verdict = classify(html, kind) if status == 200 else "error"
        row = {"status": verdict, "snapshot_ts": served, "snapshot_url": snapshot_url_for(served, path), "html": html}
        if verdict == "ok":
            return row
        if verdict == "closing-page" and (fallback is None or fallback["status"] != "closing-page"):
            fallback = row
        elif fallback is None:
            fallback = row
    return fallback or {"status": "error", "snapshot_ts": "", "snapshot_url": "", "html": ""}


def player_urls_from_team_pages(store: Store) -> list[str]:
    """Every /players/<slug> page linked from the stored team pages' roster and buyout tables."""
    paths: set[str] = set()
    for p in store.wayback_pages(kind="team"):
        paths.update(team_page_player_paths(gzip.decompress(p["html"]).decode("utf-8", "replace")))
    return [page_url(p) for p in sorted(paths)]


def collect(store: Store, client: Client, *, kinds: Iterable[str] = ("team", "player"), max_pages: int | None = None,
            pause: float = 2.5, backoff: tuple[float, ...] = (10, 30, 90), teams: Iterable[str] | None = None,
            retry_statuses: Iterable[str] = ("error",), max_attempts: int = 3,
            sleep: Callable[[float], None] = time.sleep, bulk_index: bool = True) -> dict[str, Any]:
    """Fetch team pages, then the player pages they link to. Pages already stored with a final
    status (ok, closing-page, missing) are skipped; ``retry_statuses`` are fetched again."""
    kinds = tuple(kinds)
    started = datetime.now(UTC).isoformat()
    run_id = store.start_run("wayback_capfriendly", started)
    wb = Wayback(client, pause=pause, backoff=backoff, sleep=sleep)
    retry = set(retry_statuses)
    statuses: Counter[str] = Counter()
    processed = 0
    failures = 0
    note = None

    def pending(urls: list[str]) -> list[str]:
        out = []
        for u in urls:
            st = store.wayback_page_status(u)
            if st is None or (st != "ok" and (st in retry or st not in DONE_STATUSES)):
                out.append(u)
        return out

    def run(kind: str, urls: list[str]) -> bool:
        nonlocal processed, failures, note
        todo = pending(urls)
        log.info("wayback %s: %d of %d pages to fetch", kind, len(todo), len(urls))
        index = None
        if todo and kind == "player" and bulk_index:
            try:
                index = wb.bulk_captures()
            except WaybackUnavailable as exc:
                log.warning("wayback bulk index unavailable (%s); falling back to per-page CDX", exc)
        for u in todo:
            if max_pages is not None and processed >= max_pages:
                note = f"stopped at max_pages={max_pages}"
                return False
            try:
                row = fetch_page(wb, u, kind, max_attempts=max_attempts, index=index)
            except WaybackUnavailable as exc:
                failures += 1
                log.error("wayback %s: %s", u, exc)
                if failures >= 3:
                    note = "stopped: archive unavailable (3 pages failed after back-off)"
                    return False
                continue
            failures = 0
            processed += 1
            statuses[row["status"]] += 1
            store.upsert_wayback_page(u, kind, row["snapshot_ts"], row["snapshot_url"],
                                      gzip.compress(row["html"].encode("utf-8")), row["status"],
                                      datetime.now(UTC).isoformat())
            store.commit()
            if processed % PROGRESS_EVERY == 0:
                log.info("wayback: %d pages fetched this run (%s), %d requests",
                         processed, dict(statuses), wb.requests)
        return True

    ok = True
    if "team" in kinds:
        ok = run("team", team_urls(teams))
    if ok and "player" in kinds:
        run("player", player_urls_from_team_pages(store))
    store.finish_run(run_id, datetime.now(UTC).isoformat(), wb.requests, processed, note)
    store.commit()
    log.info("wayback: %d pages stored (%s), %d requests%s", processed, dict(statuses), wb.requests,
             f"; {note}" if note else "")
    return {"requests": wb.requests, "new": processed, "statuses": dict(statuses), "note": note}


# --------------------------------------------------------------------------
# Historical mode: team pages before past trade deadlines (2019-20 .. 2023-24).
#
# For each team and each target date, the last valid capture strictly before
# that date (UTC midnight) is stored as kind ``team_deadline`` under the key
# ``<team url>#before=<YYYY-MM-DD>``, apart from the July 2024 ``team`` pages.
# Targets per season are the candidacy backtest's as-of dates (Jan 1, Feb 1,
# deadline - 14 days) and the deadline itself, so a consumer can always pick a
# roster captured on or before its own as-of date (no look-ahead).
#
# One CDX query per team lists every capture since July 2019; snapshots are
# fetched with the same pacing and back-off as the 2024 crawl. A capture is
# valid when it carries the roster header ("Years Remaining", upper case on the
# 2019-2023 layout), Wayback served a capture before the target (``id_`` can
# redirect to a neighbouring one), and its first season column is the target's
# season. A snapshot fetched for one target is reused for the others.

DEADLINE_KIND = "team_deadline"
# Published NHL trade deadlines, by season start year.
DEADLINES: dict[int, date] = {
    2019: date(2020, 2, 24),
    2020: date(2021, 4, 12),
    2021: date(2022, 3, 21),
    2022: date(2023, 3, 3),
    2023: date(2024, 3, 8),
}
FIRST_SEASON: dict[str, int] = {"kraken": 2021}  # Seattle's first NHL season
HISTORICAL_SLUGS = tuple(s for s in TEAM_SLUGS if s != "utah")  # Utah's page starts in 2024
SEASON_HEADER_RE = re.compile(r"years remaining.*?(20\d\d)-(\d\d)", re.I | re.S)


def deadline_targets(season: int) -> list[date]:
    """The dates a capture must precede for one season, earliest first."""
    dl = DEADLINES[season]
    return sorted({date(season + 1, 1, 1), date(season + 1, 2, 1), dl - timedelta(days=14), dl})


def deadline_key(team_url: str, target: date) -> str:
    return f"{team_url}#before={target.isoformat()}"


def target_of(key: str) -> date | None:
    m = re.search(r"#before=(\d{4}-\d\d-\d\d)$", key)
    return date.fromisoformat(m.group(1)) if m else None


def season_of(target: date) -> int:
    """Season start year a target date falls in (July 1 boundary)."""
    return target.year if target.month >= 7 else target.year - 1


def page_season(html: str) -> int | None:
    """Start year of the first season column after the roster header."""
    m = SEASON_HEADER_RE.search(html)
    return int(m.group(1)) if m else None


def classify_deadline(html: str, served_ts: str, target: date) -> str:
    """ok | stale (a capture of an earlier season) | late (served at or after the target) | error."""
    if served_ts[:8] >= target.strftime("%Y%m%d"):
        return "late"
    if "years remaining" not in html.lower():
        return "error"
    season = page_season(html)
    if season is None:
        return "error"
    return "ok" if season == season_of(target) else "stale"


def deadline_plan(seasons: Iterable[int], slugs: Iterable[str]) -> dict[str, list[date]]:
    """{team url: [target dates]} for the seasons each team played."""
    plan: dict[str, list[date]] = {}
    for slug in slugs:
        targets = [t for s in sorted(seasons) if s >= FIRST_SEASON.get(slug, 0) for t in deadline_targets(s)]
        if targets:
            plan[page_url(f"/teams/{slug}")] = targets
    return plan


def collect_deadline(store: Store, client: Client, *, seasons: Iterable[int] = tuple(DEADLINES),
                     teams: Iterable[str] | None = None, max_pages: int | None = None, pause: float = 2.5,
                     backoff: tuple[float, ...] = (10, 30, 90), max_attempts: int = 4,
                     retry_statuses: Iterable[str] = ("error",),
                     sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Fetch the last valid team page before every target date (see above). Resumable: a key
    already stored with ok or missing is skipped; ``retry_statuses`` are tried again.
    ``max_pages`` caps the snapshots fetched in one run."""
    started = datetime.now(UTC).isoformat()
    run_id = store.start_run("wayback_capfriendly_deadline", started)
    wb = Wayback(client, pause=pause, backoff=backoff, sleep=sleep)
    retry = set(retry_statuses)
    plan = deadline_plan(seasons, teams or HISTORICAL_SLUGS)
    statuses: Counter[str] = Counter()
    fetched = 0
    failures = 0
    note = None

    def done(key: str) -> bool:
        st = store.wayback_page_status(key)
        return st is not None and (st == "ok" or (st not in retry and st in ("ok", "missing")))

    for url, targets in plan.items():
        todo = [t for t in targets if not done(deadline_key(url, t))]
        if not todo:
            continue
        # snapshots already stored for this team, reused across targets and runs
        cache: dict[str, str] = {}
        for p in store.wayback_pages(kind=DEADLINE_KIND):
            if p["url"].startswith(url + "#") and p["snapshot_ts"]:
                cache[p["snapshot_ts"]] = gzip.decompress(p["html"]).decode("utf-8", "replace")
        lo = f"{season_of(min(todo))}0701"
        hi = (max(todo) - timedelta(days=1)).strftime("%Y%m%d")
        try:
            caps = wb.captures(url, lo, hi)
        except WaybackUnavailable as exc:
            failures += 1
            log.error("wayback deadline CDX %s: %s", url, exc)
            if failures >= 3:
                note = "stopped: archive unavailable (3 requests failed after back-off)"
                break
            continue
        caps.sort(key=lambda r: r[0], reverse=True)
        stop = False
        for target in todo:
            key = deadline_key(url, target)
            cutoff = target.strftime("%Y%m%d")
            floor = f"{season_of(target)}0701"
            cands = [c for c in caps if floor <= c[0][:8] < cutoff]
            row: dict[str, Any] = {"status": "missing", "snapshot_ts": "", "snapshot_url": "", "html": ""}
            tried = 0
            for ts, original, _length in cands:
                if tried >= max_attempts:
                    break
                if ts in cache:
                    served, html, status = ts, cache[ts], 200
                else:
                    if max_pages is not None and fetched >= max_pages:
                        note = f"stopped at max_pages={max_pages}"
                        stop = True
                        break
                    tried += 1
                    try:
                        status, html, served = wb.snapshot(ts, original)
                    except WaybackUnavailable as exc:
                        failures += 1
                        log.error("wayback %s @%s: %s", url, ts, exc)
                        if failures >= 3:
                            note = "stopped: archive unavailable (3 requests failed after back-off)"
                            stop = True
                            break
                        continue
                    failures = 0
                    fetched += 1
                    if status == 200:
                        cache[served] = html
                verdict = classify_deadline(html, served, target) if status == 200 else "error"
                if verdict == "ok":
                    row = {"status": "ok", "snapshot_ts": served,
                           "snapshot_url": snapshot_url_for(served, url.removeprefix(CF_BASE)), "html": html}
                    break
                if verdict == "stale":
                    break  # older captures are older still
                row = {"status": "error", "snapshot_ts": "", "snapshot_url": "", "html": ""}
            if stop:
                break
            statuses[row["status"]] += 1
            store.upsert_wayback_page(key, DEADLINE_KIND, row["snapshot_ts"], row["snapshot_url"],
                                      gzip.compress(row["html"].encode("utf-8")), row["status"],
                                      datetime.now(UTC).isoformat())
            store.commit()
        log.info("wayback deadline %s: %s so far, %d snapshots fetched, %d requests", url.removeprefix(CF_BASE),
                 dict(statuses), fetched, wb.requests)
        if stop:
            break
    store.finish_run(run_id, datetime.now(UTC).isoformat(), wb.requests, fetched, note)
    store.commit()
    log.info("wayback deadline: %s, %d snapshots fetched, %d requests%s", dict(statuses), fetched, wb.requests,
             f"; {note}" if note else "")
    return {"requests": wb.requests, "fetched": fetched, "statuses": dict(statuses), "note": note}
