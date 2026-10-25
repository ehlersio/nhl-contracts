"""Cap charges that are not roster contracts: salary retained on a traded player
and buyout charges ("dead cap"), from PuckPedia's public Bluesky posts (tier D),
plus the merge of those claims with the archived CapFriendly team-page rows
(tier E, ``capfriendly.parse_team_dead_cap``).

PuckPedia writes every trade as blocks of what each team received::

    🚨Trade

    To #stlblues
    Cam Fowler, Yr 7/8 $6.5M ($2.5M retained)
    '27 4th

    To #Flytogether
    Jeremie Biakabutuka, Yr 2/3 $825K

and a buyout as a header plus the charge per year::

    The #SJSharks bought out the final 1 year @ $7M Cap Hit of 38 y/o LD Vlasic.

    Buyout Cap Hit:
    Year 1 $4.67M
    Year 2 $1.17M

Retention lines come in a few shapes: ``($2.5M retained)``, ``(50% retained)``,
``(50%) retain``, ``- 50% retained``, ``- $800K retained``, ``(15% retained by
BOS)``, ``($1.5M retained previously by PHI)`` and the In/Out format ``Retained
$4.625M on Rantanen``. A "previously retained" note repeats a charge an earlier
trade created (flag ``previous_retention``); its retaining team is only taken
from "by". The retaining team is the one named by
"by", else the other block of the trade, else the post's team. The charge is the
stated amount, or the share of the player's cap hit on the line. ``Yr 7/8`` gives
the seasons left (two, the current one included); without it only the trade's
season is emitted, flagged ``end_season_unknown``, and :func:`extend_retentions`
fills the rest from the player's merged contract. Nothing is invented: a line
without a cap hit and without a retained amount yields a row with a null charge
and the percentage only.

Cap seasons start July 1; a June trade or buyout (draft week, the first buyout
window) charges the coming season, so the season of an event dated June or later
is the one starting that summer (``cap_season``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from . import puckpedia as pp
from .model import DeadCapClaim, Source, claim_id
from .text import name_key, shift_season

TIER = "D"

TO_BLOCK = re.compile(r"^(?:To|In)\s*:?\s*#(?P<tag>[A-Za-z][A-Za-z0-9]*)", re.I)
RETAIN_PCT = re.compile(
    r"(?P<pct>\d{1,2}(?:\.\d+)?)\s*%\s*\)?\s*-?\s*(?:retained|retain|retention)\b"
    r"(?:\s+by\s+(?:the\s+)?#?(?P<by>[A-Za-z]{2,4})\b)?", re.I)
RETAIN_AMT = re.compile(
    r"\$(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?\s+(?:prev(?:iously)?\s+)?retained\b"
    r"(?:\s+previously)?(?:\s+by\s+(?:the\s+)?#?(?P<by>[A-Za-z]{2,4})\b)?", re.I)
PREVIOUS = re.compile(r"\bprev(?:iously)?\b", re.I)
RETAIN_ON = re.compile(
    r"retained\s+\$?(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?[ \t]+on[ \t]+"
    r"(?P<name>[A-Z][\w'.-]+(?:[ \t]+[A-Z][\w'.-]+)*)", re.I)
YR_OF = re.compile(r"\bYr\s*(?P<a>\d)\s*/\s*(?P<b>\d)\b", re.I)
PAREN = re.compile(r"\([^)]*\)")
BUYOUT_HEAD = re.compile(r"\bbought out\b|\bbuy ?out\b", re.I)
BUYOUT_YEAR = re.compile(
    r"^(?:Year|Yr)\s*(?P<n>\d)\s*[:\-]?\s*\$?(?P<num>\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(?P<unit>[KkMm])?", re.I)
BUYOUT_LINK = re.compile(r"puckpedia\.com/player/(?P<slug>[a-z0-9-]+)/buyout", re.I)
BUYOUT_NAME = re.compile(
    r"(?:\bput\s+(?P<put>[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)*)\s+on\s+waivers"
    r"|\bof\s+(?:\d+\s*y/o\s+)?(?:[A-Z]{1,2}\s+)?(?P<of>[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)*)\s*[.!]?\s*$)")
NAME_HEAD = re.compile(r"^(?P<name>[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)*)")
TRI = re.compile(r"^[A-Z]{2,4}$")


def cap_season(d: date) -> str:
    y = d.year if d.month >= 6 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def _amount(num: str, unit: str | None) -> int:
    n = float(num.replace(",", ""))
    u = (unit or "").lower()
    if u == "m":
        return int(round(n * 1_000_000))
    if u == "k":
        return int(round(n * 1_000))
    if "," in num or n >= 1000:
        return int(round(n))
    return int(round(n * 1_000_000)) if n < 20 else int(round(n * 1_000))


def _source(post: dict[str, Any], head: str) -> Source:
    return Source(
        source_id=f"bsky:{pp.rkey_of(post['uri'])}", url=pp.post_url(post["uri"]), publisher=pp.PUBLISHER,
        author="PuckPedia", published_at=post.get("created_at") or "", confidence_tier=TIER,
        headline=head[:200], kind=pp.KIND,
    )


def _claim(source: Source, kind: str, name: str, team: str | None, counterparty: str | None, season: str,
           cap_hit: int | None, pct: float | None, event: date, flags: list[str], evidence: str) -> DeadCapClaim:
    return DeadCapClaim(
        claim_id=claim_id(f"{source.source_id}#{kind}:{team or '?'}:{season}", name, None), kind=kind,
        player_name=name, player_id=None, team=team, counterparty=counterparty, season=season,
        cap_hit_usd=cap_hit, pct=pct, contract_key=None, event_date=event.isoformat(),
        source_id=source.source_id, confidence_tier=TIER, flags=list(dict.fromkeys(flags)),
        evidence=evidence[:300],
    )


def _line_cap_hit(line: str) -> int | None:
    """The player's cap hit on a trade line: the first amount outside parentheses and
    not itself the retained amount ("$1.95M - $800K retained")."""
    bare = RETAIN_AMT.sub(" ", PAREN.sub(" ", line))
    for m in pp._amounts(bare):
        if m["dollar"] or m["unit"] or m["word"]:
            return pp.money(m)[0]
    return None


def _line_name(line: str) -> str | None:
    m = NAME_HEAD.match(line.strip())
    if not m:
        return None
    name = m["name"].strip(" ,")
    return name if len(name.split()) <= 4 and not name.lower().startswith(("to ", "in ", "out ")) else None


def retention_rows(post: dict[str, Any]) -> tuple[list[DeadCapClaim], Source | None]:
    """Salary retained in one trade post -> one claim per (retaining team, player, season)."""
    raw = post.get("text") or ""
    if pp.classify_post(raw) != "trade" and not RETAIN_ON.search(raw):
        return [], None
    text = pp.normalize_text(raw)
    lines = pp._lines(text)
    if not lines:
        return [], None
    head = lines[0]
    published = date.fromisoformat((post.get("created_at") or "")[:10])
    season0 = cap_season(published)
    source = _source(post, head)
    head_teams, _ = pp.team_tags(head)

    blocks: list[tuple[str | None, list[str]]] = []
    for ln in lines[1:]:
        m = TO_BLOCK.match(ln)
        if m:
            teams, _ = pp.team_tags(ln)
            # "#VGK": a bare tri-code hashtag the fan-tag map does not list
            tag = m["tag"]
            team = teams[0] if teams else (tag.upper() if tag.isupper() and TRI.match(tag) else None)
            blocks.append((team, []))
            rest = ln[m.end():].strip(" :")
            if rest:
                blocks[-1][1].append(rest)
            continue
        if blocks:
            blocks[-1][1].append(ln)
    block_teams = [t for t, _ in blocks]
    out: list[DeadCapClaim] = []

    for idx, (acquirer, body) in enumerate(blocks):
        others = [t for j, t in enumerate(block_teams) if j != idx and t]
        for ln in body:
            m_pct = RETAIN_PCT.search(ln)
            m_amt = RETAIN_AMT.search(ln)
            if not (m_pct or m_amt):
                continue
            name = _line_name(ln)
            if not name:
                continue
            flags: list[str] = []
            cap = _line_cap_hit(ln)
            pct = float(m_pct["pct"]) if m_pct else None
            charge = _amount(m_amt["num"], m_amt["unit"]) if m_amt else None
            if charge is None and pct is not None and cap is not None:
                charge = int(round(cap * pct / 100))
                flags.append("derived:charge_from_pct")
            if pct is None and charge is not None and cap:
                pct = round(charge / cap * 100, 1)
                flags.append("derived:pct_from_amounts")
            if charge is None:
                flags.append("charge_unknown")
            by = next((m["by"] for m in (m_pct, m_amt) if m and m["by"]), None)
            # "($4.625M prev retained)": a charge an earlier trade created; the team
            # trading him now is not the one carrying it.
            previous = bool(PREVIOUS.search(PAREN.search(ln).group(0) if PAREN.search(ln) else ln))
            if previous:
                flags.append("previous_retention")
            if by:
                retainer = by.upper() if TRI.match(by.upper()) else None
                if retainer is None:
                    flags.append(f"retainer_unmapped:{by}")
            elif len(others) == 1 and not previous:
                retainer = others[0]
            else:
                retainer = None
                flags.append("retainer_unknown")
            yr = YR_OF.search(ln)
            if yr and int(yr["b"]) >= int(yr["a"]):
                seasons = [shift_season(season0, i) for i in range(int(yr["b"]) - int(yr["a"]) + 1)]
            else:
                seasons = [season0]
                flags.append("end_season_unknown")
            for sn in seasons:
                out.append(_claim(source, "retained", name, retainer, acquirer, sn, charge, pct, published, flags,
                                  f"trade line: {ln}"))

    # In/Out format: "Retained $4.625M on Rantanen" charged to the post's team.
    for m in RETAIN_ON.finditer(text):
        name = m["name"].strip()
        team = head_teams[0] if head_teams else None
        flags = ["end_season_unknown", "retained_on_format"] + ([] if team else ["retainer_unknown"])
        out.append(_claim(source, "retained", name, team, None, season0, _amount(m["num"], m["unit"]), None,
                          published, flags, f"retained on: {m.group(0)}"))
    return out, (source if out else None)


def buyout_rows(post: dict[str, Any]) -> tuple[list[DeadCapClaim], Source | None]:
    """A buyout post -> one claim per charged season, as PuckPedia lists them."""
    raw = post.get("text") or ""
    text = pp.normalize_text(raw)
    lines = pp._lines(text)
    if not lines or not BUYOUT_HEAD.search(lines[0]):
        return [], None
    years = [(int(m["n"]), _amount(m["num"], m["unit"])) for m in (BUYOUT_YEAR.match(ln) for ln in lines[1:]) if m]
    if not years:
        return [], None
    head = lines[0]
    published = date.fromisoformat((post.get("created_at") or "")[:10])
    season0 = cap_season(published)
    source = _source(post, head)
    teams, _ = pp.team_tags(head)
    team = teams[0] if teams else None
    flags: list[str] = [] if team else ["team_unmapped"]
    links = list(post.get("links") or []) + pp.LINK.findall(raw)
    name = None
    for u in links:
        m = BUYOUT_LINK.search(u)
        if m:
            name = " ".join(w.capitalize() for w in m["slug"].split("-"))
            flags.append("name_from_link")
            break
    if not name:
        m = BUYOUT_NAME.search(head)
        name = (m["put"] or m["of"]) if m else None
    if not name:
        return [], None
    out = [_claim(source, "buyout", name, team, None, shift_season(season0, n - 1), amt, None, published, flags,
                  f"buyout: {head}")
           for n, amt in years]
    return out, source


def extract_post(post: dict[str, Any]) -> tuple[list[DeadCapClaim], Source | None]:
    claims, source = buyout_rows(post)
    if claims:
        return claims, source
    return retention_rows(post)


def normalize_posts(posts: Iterable[dict[str, Any]]) -> tuple[list[DeadCapClaim], list[Source]]:
    claims: list[DeadCapClaim] = []
    sources: list[Source] = []
    for post in posts:
        c, src = extract_post(post)
        claims += c
        if src is not None:
            sources.append(src)
    return claims, sources


# --- merge -----------------------------------------------------------------------

TIER_RANK = {t: i for i, t in enumerate("ABCDE")}


def _who(c: DeadCapClaim) -> str:
    return str(c.player_id) if c.player_id else name_key(c.player_name)


def extend_retentions(claims: list[DeadCapClaim], contracts: list[dict[str, Any]]) -> int:
    """For retention claims whose span the post did not state, add the later seasons
    through the end of the player's merged contract covering the trade season, and
    link the contract key. Returns the number of claims added."""
    by_player: dict[str, list[dict[str, Any]]] = {}
    for r in contracts:
        if not (r.get("start_season") and r.get("end_season")):
            continue
        for key in filter(None, (str(r.get("player_id") or ""), name_key(r.get("player_name") or ""))):
            by_player.setdefault(key, []).append(r)
    added: list[DeadCapClaim] = []
    seen = {(c.source_id, c.kind, c.team, _who(c), c.season) for c in claims}
    for c in claims:
        if c.kind != "retained" or "end_season_unknown" not in c.flags:
            continue
        cands = [r for r in by_player.get(_who(c), []) if r["start_season"] <= c.season <= r["end_season"]]
        if not cands:
            continue
        cands.sort(key=lambda r: (TIER_RANK.get(r.get("best_tier"), 9), r["end_season"]))
        deal = cands[0]
        c.contract_key = deal["contract_key"]
        c.flags.append("derived:span_from_contract")
        sn = c.season
        while sn < deal["end_season"]:
            sn = shift_season(sn, 1)
            if (c.source_id, c.kind, c.team, _who(c), sn) in seen:
                continue
            seen.add((c.source_id, c.kind, c.team, _who(c), sn))
            added.append(DeadCapClaim(
                claim_id=claim_id(f"{c.source_id}#{c.kind}:{c.team or '?'}:{sn}", c.player_name, c.player_id),
                kind=c.kind, player_name=c.player_name, player_id=c.player_id, team=c.team,
                counterparty=c.counterparty, season=sn, cap_hit_usd=c.cap_hit_usd, pct=c.pct,
                contract_key=c.contract_key, event_date=c.event_date, source_id=c.source_id,
                confidence_tier=c.confidence_tier, flags=list(c.flags), evidence=c.evidence,
            ))
    claims.extend(added)
    return len(added)


def _first(cs: list[DeadCapClaim], attr: str) -> Any:
    return next((getattr(c, attr) for c in cs if getattr(c, attr)), None)


def merge_dead_cap(claims: list[DeadCapClaim]) -> list[dict[str, Any]]:
    """One row per (team, kind, player, season): the best-tier charge, with the other
    tiers' disagreement named in ``conflicts``."""
    groups: dict[tuple[str | None, str, str, str], list[DeadCapClaim]] = {}
    for c in claims:
        groups.setdefault((c.team, c.kind, _who(c), c.season), []).append(c)
    rows = []
    for (team, kind, _who_key, season), cs in groups.items():
        cs.sort(key=lambda c: TIER_RANK.get(c.confidence_tier, 9))
        stated = [c for c in cs if c.cap_hit_usd is not None]
        charges = {c.cap_hit_usd for c in stated}
        best = stated[0] if stated else cs[0]
        conflicts = []
        if len(charges) > 1:
            conflicts.append("cap_hit_usd")
        pcts = {c.pct for c in cs if c.pct is not None}
        if len(pcts) > 1:
            conflicts.append("pct")
        # The best tier's charge stands unless two sources of that same tier disagree.
        best_tier_charges = {c.cap_hit_usd for c in stated if c.confidence_tier == best.confidence_tier}
        charge = best.cap_hit_usd if len(best_tier_charges) <= 1 else None
        rows.append({
            "team": team, "kind": kind, "player_name": best.player_name, "player_id": _first(cs, "player_id"),
            "counterparty": _first(cs, "counterparty"), "season": season,
            "cap_hit_usd": charge,
            "pct": best.pct if best.pct is not None else _first(cs, "pct"),
            "contract_key": _first(cs, "contract_key"), "event_date": _first(cs, "event_date"),
            "conflicts": ";".join(conflicts), "n_sources": len({c.source_id for c in cs}),
            "best_tier": cs[0].confidence_tier, "source_ids": ";".join(sorted({c.source_id for c in cs})),
            "flags": ";".join(sorted({f for c in cs for f in c.flags})),
        })
    rows.sort(key=lambda r: (r["team"] or "~", r["season"], r["kind"], r["player_name"]))
    return rows
