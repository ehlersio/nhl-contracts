"""Collect PuckPedia's public Bluesky feed and store the raw posts.

PuckPedia posts every NHL signing (with the year-by-year structure, trade
protection and agent), every trade and most waiver/LTIR moves to its public
Bluesky account. The AppView endpoint needs no authentication and pages
newest-first with a cursor (100 posts per page; back to 2023-07 is roughly 60
pages). Reposts and posts by other accounts are skipped. Re-runs are
idempotent: posts are keyed by their ``at://`` URI.

Modes:
- incremental (``since`` is None): page until a page adds nothing new;
- backfill (``since`` given): page until posts older than ``since`` appear.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime
from typing import Any

from ..http import Client
from ..store import Store

log = logging.getLogger(__name__)

FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
PAGE_SIZE = 100
LINK_FACET = "app.bsky.richtext.facet#link"


def post_links(record: dict[str, Any]) -> list[str]:
    """Full URLs of a post: link facets (the text shows them shortened) and the link card."""
    links: list[str] = []
    for facet in record.get("facets") or []:
        for feature in facet.get("features") or []:
            if feature.get("$type") == LINK_FACET and feature.get("uri"):
                links.append(feature["uri"])
    embed = record.get("embed") or {}
    for ext in (embed.get("external"), (embed.get("media") or {}).get("external")):
        if ext and ext.get("uri"):
            links.append(ext["uri"])
    return list(dict.fromkeys(links))


def feed_rows(payload: dict[str, Any], author: str) -> tuple[list[dict[str, Any]], str | None]:
    """Rows for ``Store.upsert_bsky_posts`` from one feed page, and the oldest createdAt seen."""
    rows: list[dict[str, Any]] = []
    oldest: str | None = None
    for item in payload.get("feed") or []:
        post = item.get("post") or {}
        record = post.get("record") or {}
        created = record.get("createdAt") or post.get("indexedAt") or ""
        if created and (oldest is None or created < oldest):
            oldest = created
        if item.get("reason") or (post.get("author") or {}).get("handle") != author:
            continue
        if not post.get("uri") or not created:
            continue
        rows.append({
            "uri": post["uri"],
            "cid": post.get("cid"),
            "author": author,
            "created_at": created,
            "text": record.get("text") or "",
            "links": post_links(record),
            "raw": record,
        })
    return rows, oldest


def collect(store: Store, client: Client, *, author: str = "puckpedia.com", since: date | None = None,
            max_pages: int | None = None, pause: float = 0.3) -> dict[str, int]:
    started = datetime.now(UTC).isoformat()
    run_id = store.start_run("bluesky_puckpedia", started)
    requests = new = pages = 0
    cursor: str | None = None
    stop = "end_of_feed"
    floor = since.isoformat() if since else None
    while True:
        params: dict[str, Any] = {"actor": author, "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = client.get_json(FEED_URL, params)
        except RuntimeError as exc:
            log.error("bluesky %s page %d failed: %s", author, pages + 1, exc)
            stop = "error"
            break
        requests += 1
        pages += 1
        rows, oldest = feed_rows(payload, author)
        if floor:
            rows = [r for r in rows if r["created_at"][:10] >= floor]
        added = store.upsert_bsky_posts(rows, datetime.now(UTC).isoformat())
        new += added
        store.commit()
        cursor = payload.get("cursor")
        if floor and oldest and oldest[:10] < floor:
            stop = "reached_since"
            break
        if floor is None and added == 0:
            stop = "no_new_rows"
            break
        if not cursor:
            break
        if max_pages and pages >= max_pages:
            stop = "max_pages"
            break
        if pause:
            time.sleep(pause)
    store.finish_run(run_id, datetime.now(UTC).isoformat(), requests, new, note=f"pages={pages} stop={stop}")
    store.commit()
    log.info("bluesky %s: %d pages, %d new posts (%s)", author, pages, new, stop)
    return {"requests": requests, "new": new, "pages": pages}
