"""Collect NHL.com stories from the Forge content API.

Every Forge list returns at most 25 items and ignores paging, so this polls a few
narrow lists per team (see ``FORGE_TEAM_LISTS``) plus the league-wide
``transactions`` list, and fetches the body of every story that looks like a
transaction or a contract. Re-runs are idempotent: a slug already in the store is
re-fetched only when Forge reports a newer ``lastUpdatedDate``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..config import FORGE_LEAGUE_LISTS, FORGE_STORIES, FORGE_TEAM_LISTS
from ..http import Client
from ..store import Store, Story

log = logging.getLogger(__name__)

# Headline/summary hints that a story may describe a contract. Deliberately broad:
# the normalizer decides what is actually a signing.
CONTRACT_HINT = re.compile(
    r"\b(sign|signs|signed|signing|re-sign|re-signs|re-signed|resign|contract|"
    r"agree[sd]?\s+to\s+terms|agreement|extension|extends?|tryout|PTO|"
    r"entry-level|two-way|one-way|arbitration|qualifying offer)\b",
    re.I,
)
TRANSACTION_TAGS = {"transactions", "press-release"}


def tag_slugs(item: dict[str, Any]) -> list[str]:
    return [t.get("slug", "") for t in item.get("tags", []) if t.get("slug")]


def author_tag(tags: list[str]) -> str | None:
    """``nhl-created`` for NHL.com staff, ``bos-created`` etc. for club releases."""
    for t in tags:
        if t.endswith("-created"):
            return t
    return None


def is_candidate(item: dict[str, Any]) -> bool:
    tags = set(tag_slugs(item))
    text = " ".join(
        str(item.get(k) or "") for k in ("headline", "title", "summary", "slug")
    )
    if CONTRACT_HINT.search(text):
        return True
    return bool(tags & TRANSACTION_TAGS)


def body_text(story: dict[str, Any]) -> str:
    parts = []
    for p in story.get("parts", []) or []:
        if p.get("type") == "markdown" and p.get("content"):
            parts.append(p["content"].strip())
    return "\n\n".join(parts)


def story_from_payload(payload: dict[str, Any], fetched_at: str) -> Story:
    tags = tag_slugs(payload)
    return Story(
        slug=payload["slug"],
        url=f"https://www.nhl.com/news/{payload['slug']}",
        content_date=payload.get("contentDate") or "",
        headline=payload.get("headline") or payload.get("title") or "",
        title=payload.get("title") or "",
        summary=payload.get("summary") or "",
        tags=tags,
        body=body_text(payload),
        author_tag=author_tag(tags),
        created_by=payload.get("createdBy"),
        entity_id=payload.get("_entityId"),
        last_updated=payload.get("lastUpdatedDate"),
        fetched_at=fetched_at,
    )


def list_queries(team_ids: list[int]) -> list[str]:
    queries = list(FORGE_LEAGUE_LISTS)
    for tid in team_ids:
        queries.extend(q.format(id=tid) for q in FORGE_TEAM_LISTS)
    return queries


def _poll_list(store: Store, client: Client, query: str, seen: set[str], counters: dict[str, int],
               max_stories: int | None) -> None:
    """Poll one Forge list, mark every item seen and fetch new or updated candidate stories.

    ``counters`` gets ``requests`` and ``new`` added in place; ``seen`` holds the slugs
    already handled in this run, so a story listed by several queries is fetched once."""
    try:
        payload = client.get_json(FORGE_STORIES, {"tags.slug": query})
    except RuntimeError as exc:
        log.error("list %s failed: %s", query, exc)
        return
    counters["requests"] += 1
    for item in payload.get("items", []):
        slug = item.get("slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        candidate = is_candidate(item)
        now = datetime.now(UTC).isoformat()
        store.mark_seen(slug, query, item.get("contentDate") or "",
                        item.get("headline") or "", candidate, now)
        if not candidate:
            continue
        known = store.story_last_updated(slug)
        if store.has_story(slug) and (known or "") >= (item.get("lastUpdatedDate") or ""):
            continue
        if max_stories is not None and counters["new"] >= max_stories:
            continue
        try:
            full = client.get_json(f"{FORGE_STORIES}/{slug}")
        except RuntimeError as exc:
            log.error("story %s failed: %s", slug, exc)
            continue
        counters["requests"] += 1
        store.upsert_story(story_from_payload(full, now))
        counters["new"] += 1
    store.commit()


def _poll(store: Store, client: Client, collector: str, queries: Iterable[str], max_stories: int | None,
          note: str | None = None) -> dict[str, int]:
    """Run bookkeeping around :func:`_poll_list` for a sequence of list queries."""
    run_id = store.start_run(collector, datetime.now(UTC).isoformat())
    counters = {"requests": 0, "new": 0}
    seen: set[str] = set()
    n_lists = 0
    for query in queries:
        _poll_list(store, client, query, seen, counters, max_stories)
        n_lists += 1
    store.finish_run(run_id, datetime.now(UTC).isoformat(), counters["requests"], counters["new"], note)
    store.commit()
    log.info("%s: %d lists, %d requests, %d new/updated stories, %d listed",
             collector, n_lists, counters["requests"], counters["new"], len(seen))
    return {**counters, "listed": len(seen), "lists": n_lists}


def collect(store: Store, client: Client, team_ids: list[int], *, max_stories: int | None = None
            ) -> dict[str, int]:
    """Poll every list, fetch new candidate stories, store them. Returns counters."""
    out = _poll(store, client, "nhl_forge", list_queries(team_ids), max_stories)
    return {k: out[k] for k in ("requests", "new", "listed")}


def player_query(player_id: int) -> str:
    return f"playerid-{player_id}"


def backfill_players(store: Store, client: Client, player_ids: Iterable[int], *,
                     max_players: int | None = None, max_stories: int | None = None) -> dict[str, int]:
    """Poll ``tags.slug=playerid-N`` (the 25 latest stories) for each player and store new
    candidate stories, with the same filter, idempotent upserts and run bookkeeping as
    :func:`collect`. Items are marked seen with ``list_query`` ``playerid-N``.

    The caller decides which players to (re-)poll: the store has no accessor that
    says when a player list was last polled (``forge_seen`` keeps the first query a
    slug was seen under, and a player with no stories leaves no row), so this does
    not skip recently polled players by itself. Returns counters plus ``players``."""
    ids = list(dict.fromkeys(int(p) for p in player_ids))
    if max_players is not None:
        ids = ids[:max_players]
    out = _poll(store, client, "nhl_forge_players", (player_query(p) for p in ids), max_stories,
                note=f"players={len(ids)}")
    return {"requests": out["requests"], "new": out["new"], "listed": out["listed"], "players": len(ids)}
