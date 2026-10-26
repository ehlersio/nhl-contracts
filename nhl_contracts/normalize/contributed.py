"""Claims contributed by hand: rows a person extracted from a cited public source
that the collectors never stored (a Forge story that aged out of the 25-item
lists before the first poll) or that the parser does not yet read.

The file uses the columns of ``claims.csv``. Each row must cite its source
(``source_id``, ``source_url``) and quote the sentence the values come from in
``evidence``. Rows carry ``extracted_by = "contributed"``.

A contributed row yields to the pipeline: when a normalizer produces a claim from
the same source about the same player (same NHL id, or the same name when the
parsed claim has no id yet), the contributed row is dropped, so fixing the parser
or collecting the story later never duplicates a claim.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .model import Claim, Source
from .text import alias_key

INT_FIELDS = ("player_id", "term_years", "aav_usd", "cap_hit_usd", "total_usd", "nhl_salary_usd",
              "minors_salary_usd")
BOOL_FIELDS = ("signing_date_stated", "one_way", "re_signed")
OPTIONAL_TEXT = ("team", "start_season", "end_season", "expiry_status", "agent")


def _bool(v: str) -> bool | None:
    if v == "":
        return None
    if v in ("True", "true", "1"):
        return True
    if v in ("False", "false", "0"):
        return False
    raise ValueError(f"not a boolean: {v!r}")


def read_claims(path: Path) -> list[Claim]:
    """Parse a contributed claims CSV. A missing file yields no claims."""
    if not path.exists():
        return []
    claims = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            for k in ("claim_id", "contract_key", "player_name", "signed_at", "source_id", "source_url",
                      "evidence"):
                if not row.get(k):
                    raise ValueError(f"{path}: row {row.get('claim_id') or row.get('player_name')} lacks {k}")
            kw: dict[str, object] = dict(row)
            for k in INT_FIELDS:
                kw[k] = int(row[k]) if row[k] else None
            for k in BOOL_FIELDS:
                kw[k] = _bool(row[k])
            kw["re_signed"] = bool(kw["re_signed"])
            kw["signing_date_stated"] = bool(kw["signing_date_stated"])
            for k in OPTIONAL_TEXT:
                kw[k] = row[k] or None
            kw["flags"] = [f for f in row["flags"].split(";") if f]
            kw["extracted_by"] = "contributed"
            claims.append(Claim(**kw))  # type: ignore[arg-type]
    return claims


def contributed_rows(path: Path, existing: list[Claim]) -> tuple[list[Claim], list[Source]]:
    """Contributed claims about a (source, player) no normalizer produced a claim for,
    and one Source per cited document. Normalized claims get roster ids only after
    this step, so a parsed claim is matched by id when it has one, else by name."""
    have: set[tuple[str, object]] = set()
    for c in existing:
        have.add((c.source_id, alias_key(c.player_name)))
        if c.player_id:
            have.add((c.source_id, c.player_id))
    claims = [c for c in read_claims(path)
              if (c.source_id, c.player_id) not in have and (c.source_id, alias_key(c.player_name)) not in have]
    sources: dict[str, Source] = {}
    for c in claims:
        sources.setdefault(c.source_id, Source(
            source_id=c.source_id, url=c.source_url, publisher=c.publisher, author=None,
            published_at=c.published_at, confidence_tier=c.confidence_tier, headline="",
            kind="contributed",
        ))
    return claims, list(sources.values())
