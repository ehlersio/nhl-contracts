"""Row types shared by every normalizer. One module so collectors for new sources
can emit the same shapes without importing the NHL.com parser.

Grain:
- ``Claim``        one (source document, player contract) row
- ``SeasonClaim``  one (source document, contract, season) row
- ``ClauseClaim``  one (source document, contract, clause) row
- ``Source``       one source document

Confidence tiers (``confidence_tier``):
- A  club press release on NHL.com          (official, first-hand)
- B  NHL.com staff story                     (official outlet, second-hand)
- C  other transaction wires such as ESPN    (second-hand, no money)
- D  reported, single-source public posts    (PuckPedia's public Bluesky feed, insiders)
- E  archived third-party database           (CapFriendly pages preserved by the Internet Archive)
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from .text import name_key

TIER_ORDER = "ABCDE"


def make_key(player_id: int | None, name: str, team: str | None, signed_at: str) -> str:
    who = str(player_id) if player_id else name_key(name)
    return f"{who}|{team or '?'}|{signed_at[:7]}"


def claim_id(source_id: str, name: str, player_id: int | None) -> str:
    return hashlib.sha1(f"{source_id}|{player_id or name_key(name)}".encode()).hexdigest()[:16]


@dataclass
class Claim:
    claim_id: str
    contract_key: str
    player_name: str
    player_id: int | None
    team: str | None
    signed_at: str              # YYYY-MM-DD: stated signing date when the source gives one, else publication date
    contract_type: str          # ELC | standard | extension | PTO
    term_years: int | None
    aav_usd: int | None
    cap_hit_usd: int | None
    total_usd: int | None
    nhl_salary_usd: int | None
    minors_salary_usd: int | None
    one_way: bool | None
    start_season: str | None
    end_season: str | None
    re_signed: bool
    source_id: str
    source_url: str
    publisher: str
    published_at: str
    confidence_tier: str
    extracted_by: str           # regex | llm
    flags: list[str] = field(default_factory=list)
    evidence: str = ""
    expiry_status: str | None = None   # UFA | RFA | 10.2(c) ... when stated
    agent: str | None = None           # player representative when the source names one
    signing_date_stated: bool = False  # True when signed_at is the stated signing date, not the publication date

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["flags"] = ";".join(self.flags)
        return d


@dataclass
class SeasonClaim:
    claim_id: str               # the Claim this season belongs to
    contract_key: str
    season: str                 # 2026-27
    cap_hit_usd: int | None
    aav_usd: int | None
    base_salary_usd: int | None
    signing_bonus_usd: int | None
    performance_bonus_usd: int | None
    total_salary_usd: int | None
    minors_salary_usd: int | None
    one_way: bool | None
    clause: str | None          # raw clause label for that season (NMC, NTC, M-NTC, "15 team NTC") when stated
    source_id: str
    confidence_tier: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClauseClaim:
    claim_id: str
    contract_key: str
    kind: str                   # NMC | NTC | M-NTC
    season_from: str | None
    season_to: str | None
    list_size: int | None       # for modified clauses: number of teams on the list
    list_type: str | None       # "no-trade" (teams the player cannot be traded to) | "approved" | None
    source_id: str
    confidence_tier: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeadCapClaim:
    """One (source document, team, player, season) cap charge that is not a roster
    contract: a buyout charge or salary retained on a traded player. ``team`` is
    the team carrying the charge; ``counterparty`` the acquiring team of a
    retained deal when the source names it. ``cap_hit_usd`` is the charge for
    ``season``; ``pct`` the retained share when stated or derivable."""

    claim_id: str
    kind: str                   # buyout | retained
    player_name: str
    player_id: int | None
    team: str | None
    counterparty: str | None
    season: str                 # 2026-27
    cap_hit_usd: int | None
    pct: float | None
    contract_key: str | None    # the underlying contract when matched
    event_date: str | None      # trade or buyout date (YYYY-MM-DD) when known
    source_id: str
    confidence_tier: str
    flags: list[str] = field(default_factory=list)
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["flags"] = ";".join(self.flags)
        return d


@dataclass
class Source:
    source_id: str
    url: str
    publisher: str
    author: str | None
    published_at: str
    confidence_tier: str
    headline: str
    kind: str                   # nhl_forge | espn_transactions | bluesky_puckpedia | wayback_capfriendly
