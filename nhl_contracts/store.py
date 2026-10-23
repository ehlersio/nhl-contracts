"""SQLite store for raw collected rows. Everything is keyed so re-runs are idempotent."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS forge_stories (
    slug TEXT PRIMARY KEY,
    entity_id TEXT,
    url TEXT NOT NULL,
    content_date TEXT NOT NULL,
    last_updated TEXT,
    headline TEXT,
    title TEXT,
    summary TEXT,
    tags TEXT NOT NULL,          -- JSON list of tag slugs
    author_tag TEXT,             -- e.g. nhl-created, bos-created
    created_by TEXT,
    body TEXT,                   -- markdown parts joined, forge-entity markup kept
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forge_seen (
    slug TEXT PRIMARY KEY,
    list_query TEXT,
    content_date TEXT,
    headline TEXT,
    candidate INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS espn_transactions (
    key TEXT PRIMARY KEY,        -- sha1 of date|team|description
    date TEXT NOT NULL,
    team_abbrev TEXT,
    team_id TEXT,
    description TEXT NOT NULL,
    raw TEXT NOT NULL,           -- original JSON row (logos stripped)
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    team TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    abbrev TEXT NOT NULL,
    full_name TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bsky_posts (
    uri TEXT PRIMARY KEY,        -- at://did/app.bsky.feed.post/rkey
    cid TEXT,
    author TEXT NOT NULL,        -- handle, e.g. puckpedia.com
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    links TEXT,                  -- JSON list of URLs found in facets/embeds
    raw TEXT NOT NULL,           -- original record JSON
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wayback_pages (
    url TEXT PRIMARY KEY,        -- original URL, e.g. https://www.capfriendly.com/players/connor-mcdavid
    kind TEXT NOT NULL,          -- team | player | browse
    snapshot_ts TEXT NOT NULL,   -- Wayback timestamp YYYYMMDDhhmmss
    snapshot_url TEXT NOT NULL,
    html BLOB NOT NULL,          -- gzip-compressed page
    status TEXT NOT NULL,        -- ok | closing-page | missing | error
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    collector TEXT NOT NULL,
    requests INTEGER DEFAULT 0,
    new_rows INTEGER DEFAULT 0,
    note TEXT
);
"""


@dataclass
class Story:
    slug: str
    url: str
    content_date: str
    headline: str
    title: str
    summary: str
    tags: list[str]
    body: str
    author_tag: str | None = None
    created_by: str | None = None
    entity_id: str | None = None
    last_updated: str | None = None
    fetched_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Story:
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k != "extra"}
        known["tags"] = list(known.get("tags") or [])
        for k in ("headline", "title", "summary", "body"):
            known[k] = known.get(k) or ""
        return cls(**known)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=120)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- forge -------------------------------------------------------------
    def has_story(self, slug: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM forge_stories WHERE slug=?", (slug,)).fetchone()
        return row is not None

    def story_last_updated(self, slug: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_updated FROM forge_stories WHERE slug=?", (slug,)
        ).fetchone()
        return row["last_updated"] if row else None

    def upsert_story(self, s: Story) -> None:
        self.conn.execute(
            """INSERT INTO forge_stories (slug, entity_id, url, content_date, last_updated,
                   headline, title, summary, tags, author_tag, created_by, body, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET entity_id=excluded.entity_id, url=excluded.url,
                   content_date=excluded.content_date, last_updated=excluded.last_updated,
                   headline=excluded.headline, title=excluded.title, summary=excluded.summary,
                   tags=excluded.tags, author_tag=excluded.author_tag,
                   created_by=excluded.created_by, body=excluded.body, fetched_at=excluded.fetched_at
            """,
            (
                s.slug, s.entity_id, s.url, s.content_date, s.last_updated, s.headline, s.title,
                s.summary, json.dumps(s.tags), s.author_tag, s.created_by, s.body, s.fetched_at,
            ),
        )

    def mark_seen(self, slug: str, list_query: str, content_date: str, headline: str,
                  candidate: bool, seen_at: str) -> None:
        self.conn.execute(
            """INSERT INTO forge_seen (slug, list_query, content_date, headline, candidate, seen_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET seen_at=excluded.seen_at""",
            (slug, list_query, content_date, headline, int(candidate), seen_at),
        )

    def stories(self) -> Iterator[Story]:
        for r in self.conn.execute("SELECT * FROM forge_stories ORDER BY content_date"):
            yield Story(
                slug=r["slug"], url=r["url"], content_date=r["content_date"],
                headline=r["headline"] or "", title=r["title"] or "", summary=r["summary"] or "",
                tags=json.loads(r["tags"]), body=r["body"] or "", author_tag=r["author_tag"],
                created_by=r["created_by"], entity_id=r["entity_id"],
                last_updated=r["last_updated"], fetched_at=r["fetched_at"],
            )

    # -- espn --------------------------------------------------------------
    def upsert_espn(self, rows: Iterable[dict[str, Any]], fetched_at: str) -> int:
        new = 0
        for r in rows:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO espn_transactions
                   (key, date, team_abbrev, team_id, description, raw, fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (r["key"], r["date"], r.get("team_abbrev"), r.get("team_id"), r["description"],
                 json.dumps(r["raw"], ensure_ascii=False), fetched_at),
            )
            new += cur.rowcount
        return new

    def espn_rows(self) -> Iterator[dict[str, Any]]:
        for r in self.conn.execute("SELECT * FROM espn_transactions ORDER BY date"):
            yield dict(r)

    # -- bluesky -----------------------------------------------------------
    def upsert_bsky_posts(self, rows: Iterable[dict[str, Any]], fetched_at: str) -> int:
        """rows: dicts with uri, cid, author, created_at, text, links (list), raw (dict)."""
        new = 0
        for r in rows:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO bsky_posts (uri, cid, author, created_at, text, links, raw, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["uri"], r.get("cid"), r["author"], r["created_at"], r["text"],
                 json.dumps(r.get("links") or []), json.dumps(r.get("raw") or {}, ensure_ascii=False), fetched_at),
            )
            new += cur.rowcount
        return new

    def bsky_posts(self, author: str | None = None) -> Iterator[dict[str, Any]]:
        q = "SELECT * FROM bsky_posts" + (" WHERE author=?" if author else "") + " ORDER BY created_at"
        for r in self.conn.execute(q, (author,) if author else ()):
            d = dict(r)
            d["links"] = json.loads(d["links"] or "[]")
            yield d

    def oldest_bsky_post(self, author: str) -> str | None:
        row = self.conn.execute("SELECT MIN(created_at) FROM bsky_posts WHERE author=?", (author,)).fetchone()
        return row[0] if row else None

    # -- wayback -----------------------------------------------------------
    def upsert_wayback_page(self, url: str, kind: str, snapshot_ts: str, snapshot_url: str,
                            html_gz: bytes, status: str, fetched_at: str) -> None:
        self.conn.execute(
            """INSERT INTO wayback_pages (url, kind, snapshot_ts, snapshot_url, html, status, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, snapshot_ts=excluded.snapshot_ts,
                   snapshot_url=excluded.snapshot_url, html=excluded.html, status=excluded.status,
                   fetched_at=excluded.fetched_at""",
            (url, kind, snapshot_ts, snapshot_url, html_gz, status, fetched_at),
        )

    def wayback_page_status(self, url: str) -> str | None:
        row = self.conn.execute("SELECT status FROM wayback_pages WHERE url=?", (url,)).fetchone()
        return row["status"] if row else None

    def wayback_pages(self, kind: str | None = None, status: str = "ok") -> Iterator[dict[str, Any]]:
        q = "SELECT url, kind, snapshot_ts, snapshot_url, html, status, fetched_at FROM wayback_pages WHERE status=?"
        args: tuple[Any, ...] = (status,)
        if kind:
            q += " AND kind=?"
            args += (kind,)
        for r in self.conn.execute(q + " ORDER BY url", args):
            yield dict(r)

    # -- teams / runs ------------------------------------------------------
    def set_teams(self, teams: dict[int, tuple[str, str]], updated_at: str) -> None:
        self.conn.executemany(
            """INSERT INTO teams (team_id, abbrev, full_name, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(team_id) DO UPDATE SET abbrev=excluded.abbrev,
               full_name=excluded.full_name, updated_at=excluded.updated_at""",
            [(tid, ab, name, updated_at) for tid, (ab, name) in teams.items()],
        )

    def set_players(self, players: dict[int, tuple[str, str]], updated_at: str) -> None:
        self.conn.executemany(
            """INSERT INTO players (player_id, full_name, team, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(player_id) DO UPDATE SET full_name=excluded.full_name, team=excluded.team,
               updated_at=excluded.updated_at""",
            [(pid, name, team, updated_at) for pid, (name, team) in players.items()],
        )

    def players(self) -> dict[int, tuple[str, str | None]]:
        return {r["player_id"]: (r["full_name"], r["team"]) for r in self.conn.execute("SELECT * FROM players")}

    def teams(self) -> dict[int, str]:
        return {r["team_id"]: r["abbrev"] for r in self.conn.execute("SELECT * FROM teams")}

    def start_run(self, collector: str, started_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, collector) VALUES (?,?)", (started_at, collector)
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: str, requests: int, new_rows: int,
                   note: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, requests=?, new_rows=?, note=? WHERE id=?",
            (finished_at, requests, new_rows, note, run_id),
        )

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("forge_stories", "forge_seen", "espn_transactions", "bsky_posts", "wayback_pages", "players",
                  "teams"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
