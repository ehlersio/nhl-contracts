"""LLM extractor used only when the regex parser finds a signing story but cannot
read its terms (about 230 stories in the current store: bonus schedules, mixed
one-way/two-way years, prose without the usual phrasing).

Provider: OpenRouter's OpenAI-compatible chat API. Nothing runs unless
``OPENROUTER_API_KEY`` is set; ``NHL_CONTRACTS_LLM_MODEL`` overrides the model
(default ``anthropic/claude-haiku-5.5``). The model may only *read* numbers: every
dollar figure, term and season it returns must appear in the story text, or the
row is dropped. Rows that pass carry ``extracted_by="llm"`` and the flag
``llm:<model>`` so consumers can filter them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..store import Story
from .text import MONEY, clean, money_value

log = logging.getLogger(__name__)

Extractor = Callable[[Story], list[dict[str, Any]] | None]
_extractor: Extractor | None = None
wanted: list[str] = []  # slugs that asked for a fallback this run
used: list[str] = []    # slugs the fallback answered this run
season_rows: dict[str, tuple[Any, list[dict[str, Any]]]] = {}  # claim_id -> (claim, verified season rows)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
ENV_KEY = "OPENROUTER_API_KEY"
ENV_MODEL = "NHL_CONTRACTS_LLM_MODEL"

PROMPT_TEMPLATE = """You are reading an NHL press release or news story about a player contract.
Return ONLY a JSON array with one object per player contract the text describes as newly
signed or agreed (not previous contracts, not coaches, executives or staff). Keys:
player_name, team_abbrev (NHL tri-code or null), contract_type (one of ELC, standard,
extension, PTO), term_years (integer or null), aav_usd, cap_hit_usd, total_usd,
nhl_salary_usd, minors_salary_usd (integers in US dollars or null), one_way (true, false
or null), start_season, end_season ("2026-27" style or null), seasons (array of
{{season, base_salary_usd, signing_bonus_usd, performance_bonus_usd, minors_salary_usd}},
only for seasons the text itemises). Use null for anything the text does not state.
Never compute, infer or round a figure the text does not state.

Headline: {headline}
Published: {published}
Text:
{body}
"""

NUMERIC_FIELDS = ("aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd", "minors_salary_usd")
SEASON_NUMERIC = ("base_salary_usd", "signing_bonus_usd", "performance_bonus_usd", "minors_salary_usd")


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
        rows = _extractor(story)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm fallback failed for %s: %s", story.slug, exc)
        return None
    if rows:
        used.append(story.slug)
    return rows


# --- verification ---------------------------------------------------------------

def stated_amounts(text: str) -> set[int]:
    """Every dollar figure the text states, as integers (so "$3.95 million" is 3950000)."""
    out: set[int] = set()
    for m in MONEY.finditer(text):
        v = money_value(m.group("num"), m.group("unit"))
        if v is not None:
            out.add(v)
    return out


def verify_row(row: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Drop any number the text does not state; drop the row when its player is not
    named in the text. Returns the cleaned row or None."""
    amounts = stated_amounts(text)
    seasons_in_text = set(re.findall(r"\b20\d\d-\d\d\b", text))
    name = str(row.get("player_name") or "").strip()
    if not name or name.split()[-1].lower() not in text.lower():
        return None
    cleaned: dict[str, Any] = {"player_name": name}
    for f in NUMERIC_FIELDS:
        v = row.get(f)
        cleaned[f] = int(v) if isinstance(v, int | float) and int(v) in amounts else None
    term = row.get("term_years")
    if isinstance(term, int | float) and 1 <= int(term) <= 8:
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
        if re.search(rf"\b({int(term)}|{words[int(term)]})[\s-]+year", text, re.I):
            cleaned["term_years"] = int(term)
        else:
            cleaned["term_years"] = None
    else:
        cleaned["term_years"] = None
    for f in ("start_season", "end_season"):
        v = row.get(f)
        cleaned[f] = v if isinstance(v, str) and v in seasons_in_text else None
    ctype = row.get("contract_type")
    cleaned["contract_type"] = ctype if ctype in ("ELC", "standard", "extension", "PTO") else "standard"
    one_way = row.get("one_way")
    cleaned["one_way"] = one_way if isinstance(one_way, bool) else None
    team = row.get("team_abbrev")
    cleaned["team_abbrev"] = team if isinstance(team, str) and re.fullmatch(r"[A-Z]{3}", team) else None
    seasons = []
    for s in row.get("seasons") or []:
        if not isinstance(s, dict) or s.get("season") not in seasons_in_text:
            continue
        srow = {"season": s["season"]}
        for f in SEASON_NUMERIC:
            v = s.get(f)
            srow[f] = int(v) if isinstance(v, int | float) and int(v) in amounts else None
        if any(srow[f] is not None for f in SEASON_NUMERIC):
            seasons.append(srow)
    cleaned["seasons"] = seasons
    if not any(cleaned[f] for f in (*NUMERIC_FIELDS, "term_years")) and not seasons:
        return None
    return cleaned


def parse_response(content: str) -> list[dict[str, Any]]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end < 0:
        return []
    data = json.loads(content[start:end + 1])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


# --- OpenRouter ---------------------------------------------------------------------

class OpenRouterExtractor:
    """Calls OpenRouter's chat completions endpoint and verifies every number.

    With a ``store`` every answer is cached by (model, slug, body) in ``llm_cache``,
    so a story is sent once, not on every hourly run."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None, store: Any = None) -> None:
        self.model = model
        self.store = store
        self.cache_hits = 0
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/ehlersio/nhl-contracts", "X-Title": "nhl-contracts"},
            timeout=timeout, transport=transport,
        )
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def cache_key(self, story: Story, text: str) -> str:
        return hashlib.sha1(f"{self.model}|{story.slug}|{text}".encode()).hexdigest()

    def __call__(self, story: Story) -> list[dict[str, Any]] | None:
        text = clean(story.body)[:12000]
        key = self.cache_key(story, text)
        if self.store is not None:
            cached = self.store.llm_cache_get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached or None
        prompt = PROMPT_TEMPLATE.format(headline=clean(story.headline), published=story.content_date[:10],
                                        body=text)
        resp = self._client.post(OPENROUTER_URL, json={
            "model": self.model, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp.raise_for_status()
        payload = resp.json()
        self.calls += 1
        usage = payload.get("usage") or {}
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)
        content = payload["choices"][0]["message"]["content"]
        rows = []
        full = f"{clean(story.headline)}\n{text}"
        for raw in parse_response(content):
            row = verify_row(raw, full)
            if row is not None:
                row["flags"] = [f"llm:{self.model}"]
                rows.append(row)
        if self.store is not None:
            self.store.llm_cache_put(key, story.slug, self.model, rows, int(usage.get("prompt_tokens") or 0),
                                     int(usage.get("completion_tokens") or 0), datetime.now(UTC).isoformat())
            self.store.commit()
        return rows or None


def configure_from_env(env: dict[str, str] | None = None, store: Any = None) -> OpenRouterExtractor | None:
    """Register an OpenRouter extractor when OPENROUTER_API_KEY is set; otherwise do nothing."""
    env = os.environ if env is None else env
    key = env.get(ENV_KEY)
    if not key:
        return None
    extractor = OpenRouterExtractor(key, env.get(ENV_MODEL, DEFAULT_MODEL), store=store)
    register(extractor)
    log.info("llm fallback enabled: OpenRouter %s", extractor.model)
    return extractor
