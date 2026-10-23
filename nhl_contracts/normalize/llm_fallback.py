"""Hook for an LLM-based extractor used when the regex parser finds a signing
story but cannot read its terms.

Nothing is wired by default: no provider, no key. To enable one, register a
callable that takes a :class:`~nhl_contracts.store.Story` and returns a list of
claim dicts (same keys as :class:`~nhl_contracts.normalize.contracts.Claim`) or
``None``. The run entrypoint only logs how many stories would have used it.

Example (not shipped)::

    from nhl_contracts.normalize import llm_fallback
    llm_fallback.register(my_extractor)   # my_extractor(story) -> list[dict] | None
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..store import Story

log = logging.getLogger(__name__)

Extractor = Callable[[Story], list[dict[str, Any]] | None]
_extractor: Extractor | None = None
wanted: list[str] = []  # slugs that asked for a fallback this run

PROMPT_TEMPLATE = """You are reading an NHL press release or news story about a player
contract. Return a JSON list with one object per contract described, with keys:
player_name, team_abbrev, contract_type (ELC|standard|extension|PTO), term_years,
aav_usd, cap_hit_usd, total_usd, one_way (true|false|null), start_season, end_season.
Use null for anything the text does not state. Do not infer money that is not stated.

Headline: {headline}
Published: {published}
Text:
{body}
"""


def register(fn: Extractor | None) -> None:
    global _extractor
    _extractor = fn


def is_enabled() -> bool:
    return _extractor is not None


def extract(story: Story) -> list[dict[str, Any]] | None:
    wanted.append(story.slug)
    if _extractor is None:
        return None
    try:
        return _extractor(story)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm fallback failed for %s: %s", story.slug, exc)
        return None
