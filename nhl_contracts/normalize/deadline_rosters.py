"""Team rosters before past trade deadlines, from archived CapFriendly team pages.

``collectors/wayback_capfriendly.collect_deadline`` stores, per team, the last
valid capture before each target date (the candidacy backtest's as-of dates and
the deadline) as kind ``team_deadline``. This module parses them into
``deadline_rosters`` (one row per player per stored page) and
``deadline_teams`` (one row per page: the team's cap and LTIR summary).

Two page layouts (checked against Carolina captures from every season):

- **2019-20 .. 2022-23:** one ``table#team``. Each position group opens with a
  ``tr.column_head`` row whose cells are upper case ("FORWARDS (13 - $51,342,785)",
  "YEARS REMAINING", "TERMS", ..., "2021-22"). Non-roster groups are either named
  "NON-ROSTER FORWARDS (12)" (2019-2021) or follow an ``<h3>NON-ROSTER</h3>`` row
  inside the same table (2021-22 on). "LONG-TERM INJURED RESERVE (n - $x)" and
  "TAXI SQUAD (n)" (2020-21) are groups too. Rows have the 2024 cell layout:
  ``tm_yrr`` "3 UFA", a Terms cell whose clause span carries the details
  tooltip, the Age tooltip with the birth date, one season cell per column
  with ``cap``/``aav``/.../``cls`` spans, and "UFA"/"RFA" in the expiry column.
- **2023-24:** the 2024 layout (``cf_teamProfileRosterSection__table``), parsed
  with the July 2024 helpers in ``capfriendly.py``; "Injured Reserve" and
  "Long-Term Injured Reserve" are separate tables there.

Both carry "PROJECTED CAP HIT", "PROJECTED LTIR USED" and "PROJECTED CAP SPACE"
in the page header. "Years Remaining" counts the current season (Aho, 2021-22:
"3 UFA" with cap cells in 2021-22..2023-24), so ``years_left`` (seasons after
this one) is one less. On the older layout an extension signed during the
contract's last season shows as "1 - 8 UFA" (Keller, Dec 2019): one season
left, then eight; ``years_remaining`` counts both and ``extension_years`` keeps
the eight (the 2024 layout already counts them together: Aho, Jan 2024, "9 UFA").
A season cell with spans but an empty ``cls`` span means the page lists no
clause for that season: ``clause_status`` "none".
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

from ..collectors.wayback_capfriendly import DEADLINE_KIND, DEADLINES, season_of, target_of
from .capfriendly import (
    PUBLISHER,
    TEAM_SLUGS,
    TIER,
    Node,
    _team_row,
    _team_tables,
    clause_tokens,
    money,
    parse_html,
    path_of,
    ts_to_date,
)
from .text import alias_key, name_key

# Groups kept, by the first header cell; anything else (Professional Tryout,
# Retained Salary, Buyout History) is skipped.
GROUP_RE = re.compile(
    r"^(?P<nonroster>non-roster\s+)?(?P<group>forwards|defen[cs]e|goalies|long-term injured reserve|"
    r"injured reserve|taxi squad)\b", re.I)
GROUP_NAMES = {"forwards": "Forwards", "defense": "Defense", "defence": "Defense", "goalies": "Goalies",
               "long-term injured reserve": "LTIR", "injured reserve": "IR", "taxi squad": "Taxi Squad"}
GROUP_TOTAL_RE = re.compile(r"\((\d+)\s*-\s*(\$[\d,]+)\)")
HEADER_NAMES = {"YEARS REMAINING": "Years Remaining", "TERMS": "Terms", "POS": "POS", "STATUS": "Status",
                "ACQUIRED": "Acquired", "AGE": "Age", "CAP%": "Cap%"}
SEASON_COL_RE = re.compile(r"^(20\d\d)-(\d\d)$")
SUMMARY_RE = {
    "projected_cap_hit": re.compile(r"PROJECTED CAP HIT\s*:\s*(-?\$[\d,]+)"),
    "projected_ltir_used": re.compile(r"PROJECTED LTIR USED\s*:\s*(-?\$[\d,]+)"),
    "projected_cap_space": re.compile(r"PROJECTED CAP SPACE\s*:\s*(-?\$[\d,]+)"),
}
FULL_CLAUSES = {"NMC", "NTC", "M-NMC"}

ROSTER_COLUMNS = (
    "season", "target", "as_of", "team", "team_slug", "player_id", "player_name", "birth_date",
    "capfriendly_path", "position", "group", "section", "status", "cap_hit_usd", "aav_usd", "two_way",
    "years_remaining", "extension_years", "years_left", "end_season", "expiry_status", "clause", "clause_status",
    "clause_detail", "clause_seasons", "ltir", "injured_reserve", "unconfirmed", "tier", "publisher",
    "snapshot_url",
)
TEAM_COLUMNS = (
    "season", "target", "as_of", "team", "team_slug", "players", "roster_players", "projected_cap_hit_usd",
    "projected_ltir_used_usd", "projected_cap_space_usd", "ltir_players", "ltir_cap_hit_usd", "tier",
    "publisher", "snapshot_url",
)


def season_label(start: int) -> str:
    return f"{start}-{str(start + 1)[2:]}"


def team_code_for(slug: str | None, season: int) -> str | None:
    """Tri-code a CapFriendly slug had in a season: the Coyotes page is Arizona's through 2023-24."""
    if slug == "coyotes" and season <= 2023:
        return "ARI"
    return TEAM_SLUGS.get(slug or "")


def _group(text: str) -> tuple[str, bool] | None:
    m = GROUP_RE.match(text.strip())
    if not m:
        return None
    return GROUP_NAMES[m.group("group").lower()], bool(m.group("nonroster"))


def _canon_header(cells: list[Node]) -> list[str]:
    out = []
    for c in cells:
        t = c.text()
        out.append(HEADER_NAMES.get(t.upper(), t))
    return out


def _legacy_groups(root: Node) -> Iterator[tuple[str, str, list[str], list[Node]]]:
    """(section, group, header, data rows) from the 2019-2023 ``table#team`` layout."""
    for table in root.iter("table"):
        if table.attrs.get("id") != "team":
            continue
        nonroster_part = False  # past the NON-ROSTER heading (a bare <td><h3> between rows)
        current: list[Any] | None = None  # [section, group, header, rows]
        for n in table.iter():
            if n.tag == "h3" and n.text().strip().upper() == "NON-ROSTER":
                if current:
                    yield tuple(current)  # type: ignore[misc]
                current = None
                nonroster_part = True
                continue
            if n.tag != "tr" or next((a for a in n.ancestors() if a.tag == "table"), None) is not table:
                continue
            cells = n.cells()
            if not cells:
                continue
            if "column_head" in n.classes:
                if current:
                    yield tuple(current)  # type: ignore[misc]
                g = _group(cells[0].text())
                current = None
                if g is not None:
                    section = "NON-ROSTER" if g[1] or nonroster_part else "ROSTER"
                    current = [section, g[0], _canon_header(cells), []]
            elif current is not None:
                current[3].append(n)
        if current:
            yield tuple(current)  # type: ignore[misc]


def _modern_groups(root: Node) -> Iterator[tuple[str, str, list[str], list[Node]]]:
    """The same from the 2024 layout (2023-24 captures)."""
    from .capfriendly import _body_rows, _header

    for section, first, table in _team_tables(root):
        g = _group(first)
        if g is None or section == "DEAD CAP":
            continue
        name, nonroster = g
        yield ("NON-ROSTER" if nonroster or section == "NON-ROSTER" else "ROSTER"), name, _header(table), \
            list(_body_rows(table))


def layout_of(html: str) -> str:
    return "2024" if "cf_teamProfileRosterSection__table" in html else "legacy"


def _summary(root: Node) -> dict[str, int | None]:
    text = root.text()
    out: dict[str, int | None] = {}
    for key, rx in SUMMARY_RE.items():
        m = rx.search(text)
        out[key] = money(m.group(1)) if m else None
    return out


def parse_deadline_page(html: str, snapshot_url: str, snapshot_ts: str, target: date | None = None
                        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(player rows, team row) for one archived team page. ``target`` is the date the
    capture was chosen to precede (from the store key), if known."""
    root = parse_html(html)
    m = re.search(r"/teams/([a-z]+)", path_of(snapshot_url))
    slug = m.group(1) if m else None
    groups = list((_modern_groups if layout_of(html) == "2024" else _legacy_groups)(root))
    season: int | None = None
    for _section, _group_name, header, _rows in groups:
        cols = [h for h in header if SEASON_COL_RE.match(h)]
        if cols:
            season = int(cols[0][:4])
            break
    if season is None:
        raise ValueError(f"no season columns on {snapshot_url}")
    team = team_code_for(slug, season)
    as_of = ts_to_date(snapshot_ts)
    common = {"season": season_label(season), "target": target.isoformat() if target else None, "as_of": as_of,
              "team": team, "team_slug": slug, "tier": TIER, "publisher": PUBLISHER, "snapshot_url": snapshot_url}
    rows: list[dict[str, Any]] = []
    ltir_players, ltir_total = 0, 0
    for section, group, header, trs in groups:
        idx = {h: i for i, h in enumerate(header)}
        season_cols = [(i, h) for i, h in enumerate(header) if SEASON_COL_RE.match(h)]
        for tr in trs:
            cells = tr.cells()
            if len(cells) < len(header):
                continue
            if not any("/players/" in a.attrs.get("href", "") for a in cells[0].iter("a")):
                continue
            r = _team_row(cells, idx, season_cols, section, group, slug, team, snapshot_url, snapshot_ts)
            rows.append(_roster_row(r, group, section, season, common))
            if rows[-1]["ltir"]:
                ltir_players += 1
                ltir_total += rows[-1]["cap_hit_usd"] or 0
    team_row = {**common, "players": len(rows), "roster_players": sum(r["section"] == "ROSTER" for r in rows),
                **{f"{k}_usd": v for k, v in _summary(root).items()},
                "ltir_players": ltir_players, "ltir_cap_hit_usd": ltir_total}
    return rows, team_row


def _roster_row(r: dict[str, Any], group: str, section: str, season: int, common: dict[str, Any]
                ) -> dict[str, Any]:
    label = season_label(season)
    by_season = {s["season"]: s for s in r["seasons"]}
    cur = by_season.get(label)
    yr = r["years_remaining"]
    expiry = (r["expiry_status"] or "").strip() or None
    extension = None
    m = re.match(r"-\s*(\d+)\s*(.*)", expiry or "")
    if m:  # "1 - 8 UFA": one season left on this deal, then an 8-season extension already signed
        extension, expiry = int(m.group(1)), m.group(2).strip() or None
        yr = (yr or 0) + extension
    if yr is not None and yr > 0:
        end = season + yr - 1
    else:  # no Years Remaining value: the run of season cells from this season
        end = None
        s = season
        while season_label(s) in by_season:
            end = s
            s += 1
    clause = normalize_clause((cur or {}).get("clause")) or ""
    status = (r["status"] or "").strip()
    clause_seasons = [[s["season"], normalize_clause(s["clause"])] for s in r["seasons"] if s.get("clause")]
    return {
        **common,
        "player_id": None,
        "player_name": r["name"],
        "birth_date": r["birth_date"],
        "capfriendly_path": r["player_path"],
        "position": r["position"],
        "group": group,
        "section": section,
        "status": status or None,
        "cap_hit_usd": (cur or {}).get("cap_hit"),
        "aav_usd": (cur or {}).get("aav"),
        "two_way": bool((cur or {}).get("two_way")),
        "years_remaining": yr,
        "extension_years": extension,
        "years_left": (end - season) if end is not None else None,
        "end_season": season_label(end) if end is not None else None,
        "expiry_status": expiry,
        "clause": clause or None,
        "clause_status": None if cur is None else ("clause" if clause_tokens(clause) else "none"),
        "clause_detail": r["clause_detail"],
        "clause_seasons": json.dumps(clause_seasons) if clause_seasons else None,
        "ltir": group == "LTIR" or status.upper().startswith("LTIR"),
        "injured_reserve": group == "IR" or status.upper() == "IR",
        "unconfirmed": "unconfirmed" in r["flags"],
    }


def normalize_clause(label: str | None) -> str | None:
    """The 2019-20 pages spell "Modified NTC"; later ones "M-NTC"."""
    if not label:
        return None
    return re.sub(r"\bModified\s+(NTC|NMC)\b", r"M-\1", label.strip())


def clause_kind(label: str | None) -> str | None:
    """full | modified | None for a page's clause label ("NMC", "M-NTC", "NMC, M-NTC")."""
    toks = set(clause_tokens(label))
    if toks & FULL_CLAUSES:
        return "full"
    if "M-NTC" in toks:
        return "modified"
    return None


def assign_ids(rows: list[dict[str, Any]], players: dict[int, tuple[str, str | None]]) -> int:
    """Fill ``player_id`` where exactly one known NHL player has the row's name (exact, then
    alias key). ``players`` is the store's roster/prospect list, so long-retired players stay
    unmatched; ``birth_date`` is there for consumers with their own player index."""
    by_name: dict[str, set[int]] = {}
    by_alias: dict[str, set[int]] = {}
    for pid, (name, _team) in players.items():
        by_name.setdefault(name_key(name), set()).add(pid)
        by_alias.setdefault(alias_key(name), set()).add(pid)
    n = 0
    for r in rows:
        for index, key in ((by_name, name_key(r["player_name"])), (by_alias, alias_key(r["player_name"]))):
            ids = index.get(key, set())
            if len(ids) == 1:
                r["player_id"] = next(iter(ids))
                n += 1
                break
    return n


def rows_from_store(store: Any, seasons: Iterable[int] | None = None
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Every stored ``ok`` deadline page -> (player rows, team rows, problems)."""
    wanted = set(seasons or DEADLINES)
    players: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    problems: list[str] = []
    for p in store.wayback_pages(kind=DEADLINE_KIND):
        target = target_of(p["url"])
        if target is None or season_of(target) not in wanted:
            continue
        html = gzip.decompress(p["html"]).decode("utf-8", "replace")
        try:
            rows, team = parse_deadline_page(html, p["snapshot_url"], p["snapshot_ts"], target)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if team["season"] != season_label(season_of(target)):
            problems.append(f"{p['url']}: page season {team['season']}")
            continue
        players.extend(rows)
        teams.append(team)
    assign_ids(players, store.players())
    players.sort(key=lambda r: (r["season"], r["target"] or "", r["team"] or "", r["player_name"]))
    teams.sort(key=lambda r: (r["season"], r["target"] or "", r["team"] or ""))
    return players, teams, problems


def write_csv(rows: list[dict[str, Any]], columns: tuple[str, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else (int(r[k]) if isinstance(r[k], bool) else r[k]))
                        for k in columns})


def write(players: list[dict[str, Any]], teams: list[dict[str, Any]], out_dir: Path) -> dict[str, int]:
    write_csv(players, ROSTER_COLUMNS, out_dir / "deadline_rosters.csv")
    write_csv(teams, TEAM_COLUMNS, out_dir / "deadline_teams.csv")
    return {"deadline_rosters": len(players), "deadline_teams": len(teams)}
