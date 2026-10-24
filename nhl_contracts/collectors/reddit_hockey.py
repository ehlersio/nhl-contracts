"""Collect contract posts from r/hockey's public Atom feeds and store the raw entries.

r/hockey post titles quote insider and club posts from X verbatim ("[Milstein]
Kirill Marchenko has agreed to terms on a six year, $75 million contract
extension ..."). Reddit's public RSS is the free, terms-compliant way to read
those quotes; X's API is paid. Each feed is one Atom document of up to 100
entries, newest first; there is no paging, so the collector reads the "new"
listing plus a search listing for signings and relies on running often.

Only entries whose title mentions a contract are stored (``CONTRACT_TITLE``).
Reddit's post flair is not in the feed and is not used. Rows live in their own
table, ``reddit_posts``, created here by :func:`ensure_schema`; re-runs are
idempotent (posts are keyed by their ``t3_`` id).

Stored per post: the title, the Reddit permalink (the cited source), the poster,
the outbound ``[link]`` URL from the entry's HTML (usually the quoted tweet, a
screenshot or the club release; ``None`` for a self post) and the raw entry as
JSON.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

from ..http import Client
from ..store import Store

log = logging.getLogger(__name__)

NEW_FEED = "https://www.reddit.com/r/hockey/new.rss?limit=100"
SEARCH_FEED = ("https://www.reddit.com/r/hockey/search.rss?q=signed+OR+extension+OR+%22agreed+to+terms%22"
               "&restrict_sr=on&sort=new&t=week&limit=100")
DEFAULT_FEEDS = (NEW_FEED, SEARCH_FEED)

ATOM = "{http://www.w3.org/2005/Atom}"
ANCHOR = re.compile(r"<a\s+href=\"(?P<href>[^\"]+)\"\s*>\s*(?P<text>[^<]*?)\s*</a>", re.I)
CONTRACT_TITLE = re.compile(
    r"\b(?:re-?)?sign(?:s|ed|ing)?\b|\bextensions?\b|\bcontracts?\b|\bagree(?:s|d)?\s+to\s+terms\b|\bPTO\b"
    r"|\bAAV\b|\bcap\s+hit\b|\d\s*x\s*\$|\binks?\b|\binked\b",
    re.I,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reddit_posts (
    id TEXT PRIMARY KEY,         -- t3_xxxxxx
    created_at TEXT NOT NULL,    -- Atom <published>
    title TEXT NOT NULL,
    author TEXT,                 -- the Reddit poster, e.g. u/eh_toque (not the quoted insider)
    permalink TEXT NOT NULL,     -- https://www.reddit.com/r/hockey/comments/...
    source_url TEXT,             -- the outbound [link] (tweet, screenshot, release); NULL for a self post
    raw TEXT NOT NULL,           -- the entry as JSON
    fetched_at TEXT NOT NULL
);
"""


def ensure_schema(store: Store) -> None:
    store.conn.executescript(SCHEMA)


def is_contract_title(title: str) -> bool:
    return bool(CONTRACT_TITLE.search(title or ""))


def _links(content_html: str) -> dict[str, str]:
    """``{"[link]": url, "[comments]": url}`` from an entry's HTML content."""
    out: dict[str, str] = {}
    for m in ANCHOR.finditer(content_html or ""):
        text = m["text"].strip()
        if text in ("[link]", "[comments]") and text not in out:
            out[text] = html.unescape(m["href"]).strip()
    return out


def parse_feed(body: bytes | str) -> list[dict[str, Any]]:
    """Every entry of a Reddit Atom feed as a row for :func:`upsert_posts` (unfiltered)."""
    root = ET.fromstring(body)
    rows: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM}entry"):
        post_id = (entry.findtext(f"{ATOM}id") or "").strip()
        title = html.unescape((entry.findtext(f"{ATOM}title") or "").strip())
        link_el = entry.find(f"{ATOM}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        author = (entry.findtext(f"{ATOM}author/{ATOM}name") or "").strip()
        published = (entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated") or "").strip()
        content = entry.findtext(f"{ATOM}content") or ""
        links = _links(content)
        permalink = links.get("[comments]") or link
        outbound = links.get("[link]")
        if outbound and outbound.rstrip("/") == permalink.rstrip("/"):
            outbound = None  # a self post links to itself
        if not post_id or not title or not permalink or not published:
            continue
        rows.append({
            "id": post_id,
            "created_at": published,
            "title": title,
            "author": re.sub(r"^/", "", author) or None,
            "permalink": permalink,
            "source_url": outbound,
            "raw": {
                "id": post_id, "title": title, "link": link, "author": author, "published": published,
                "updated": entry.findtext(f"{ATOM}updated"), "content": content,
            },
        })
    return rows


def upsert_posts(store: Store, rows: list[dict[str, Any]], fetched_at: str) -> int:
    """Insert new posts (INSERT OR IGNORE on id). Returns the number of new rows."""
    ensure_schema(store)
    new = 0
    for r in rows:
        cur = store.conn.execute(
            """INSERT OR IGNORE INTO reddit_posts (id, created_at, title, author, permalink, source_url, raw,
                                                   fetched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (r["id"], r["created_at"], r["title"], r.get("author"), r["permalink"], r.get("source_url"),
             json.dumps(r.get("raw") or {}, ensure_ascii=False), fetched_at),
        )
        new += cur.rowcount
    return new


def posts(store: Store) -> list[dict[str, Any]]:
    """Every stored post, oldest first, ``raw`` decoded."""
    ensure_schema(store)
    out = []
    for r in store.conn.execute("SELECT * FROM reddit_posts ORDER BY created_at, id"):
        d = dict(r)
        d["raw"] = json.loads(d["raw"] or "{}")
        out.append(d)
    return out


def collect(store: Store, client: Client, *, feeds: tuple[str, ...] = DEFAULT_FEEDS,
            pause: float = 1.0) -> dict[str, int]:
    ensure_schema(store)
    started = datetime.now(UTC).isoformat()
    run_id = store.start_run("reddit_hockey", started)
    requests = new = entries = kept = errors = 0
    for i, url in enumerate(feeds):
        if i and pause:
            time.sleep(pause)
        try:
            status, body, _final = client.get_bytes(url)
        except RuntimeError as exc:
            log.error("reddit feed %s failed: %s", url, exc)
            errors += 1
            continue
        requests += 1
        if status != 200:
            log.error("reddit feed %s returned HTTP %d", url, status)
            errors += 1
            continue
        try:
            rows = parse_feed(body)
        except ET.ParseError as exc:
            log.error("reddit feed %s is not Atom: %s", url, exc)
            errors += 1
            continue
        entries += len(rows)
        rows = [r for r in rows if is_contract_title(r["title"])]
        kept += len(rows)
        new += upsert_posts(store, rows, datetime.now(UTC).isoformat())
        store.commit()
    note = f"feeds={len(feeds)} entries={entries} contract_titles={kept} errors={errors}"
    store.finish_run(run_id, datetime.now(UTC).isoformat(), requests, new, note=note)
    store.commit()
    log.info("reddit r/hockey: %s, %d new posts", note, new)
    return {"requests": requests, "new": new, "entries": entries, "contract_titles": kept, "errors": errors}
