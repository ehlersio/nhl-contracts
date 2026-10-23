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
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ..http import Client
from ..normalize.capfriendly import CF_BASE, TEAM_SLUGS, snapshot_ts_of, snapshot_url_for, team_page_player_paths
from ..store import Store

log = logging.getLogger(__name__)

CDX = "https://web.archive.org/cdx/search/cdx"
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

    def captures(self, url: str) -> list[tuple[str, str, int | None]]:
        bare = url.split("://", 1)[-1].removeprefix("www.")
        params = {"url": bare, "output": "json", "from": "2024", "to": "2024", "filter": "statuscode:200",
                  "fl": "timestamp,original,length", "collapse": "timestamp:8"}
        status, body, _final = self.get(CDX, params)
        if status != 200 or not body.strip():
            return []
        try:
            return parse_cdx(json.loads(body))
        except ValueError:
            log.warning("wayback CDX for %s returned non-JSON", url)
            return []

    def snapshot(self, ts: str, original: str) -> tuple[int, str, str]:
        """(status, html, timestamp actually served: Wayback may redirect to a neighbouring capture)."""
        status, body, final = self.get(SNAPSHOT.format(ts=ts, url=original))
        return status, body.decode("utf-8", "replace"), snapshot_ts_of(final) or ts


def fetch_page(wb: Wayback, url: str, kind: str, *, max_attempts: int = 3) -> dict[str, Any]:
    """Choose and fetch the best snapshot of one page. Returns the row to store."""
    path = url.removeprefix(CF_BASE)
    ranked = rank_snapshots(wb.captures(url))
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
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
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
        for u in todo:
            if max_pages is not None and processed >= max_pages:
                note = f"stopped at max_pages={max_pages}"
                return False
            try:
                row = fetch_page(wb, u, kind, max_attempts=max_attempts)
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
