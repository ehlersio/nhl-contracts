"""Parse archived CapFriendly pages (Internet Archive snapshots) into claim rows.

CapFriendly shut down in July 2024. Its team and player pages survive as Wayback
snapshots; ``collectors/wayback_capfriendly.py`` stores them in ``wayback_pages``.
Everything here is a pure function over HTML strings, with a small stdlib tree
builder (no third-party HTML parser is a dependency of this package).

Page facts the parsers rely on (checked against the July 2024 fixtures):

- Team pages: one table per position group, first header cell "Forwards (13 -
  $53,350,000)", "Defense (7)", "Goalies (3)". The NHL roster comes first, the
  minor-league and junior tables follow under an ``<h3>NON-ROSTER</h3>``; the
  buyout table ("Buyout History (2)") sits under ``<h3>DEAD CAP</h3>``. Season
  cells hold one span per view: ``cap``, ``aav``, ``sal``, ``bsal``, ``pbonus``,
  ``sbonus``, ``cls``. In two-way cells (class ``twoway``, the non-roster tables)
  ``sal``/``bsal`` show the MINOR-LEAGUE salary, not the NHL one (Bourgault:
  team page $80,000, player page Base Salary $832,500 / Minors Sal $80,000).
- The Terms column shows the clause label ("M-NTC, NMC"); its ``title`` tooltip
  holds the details, e.g. "Modified No-Trade Clause, No-Move Clause - (NMC and
  Modified NTC starting July 2022. Player submits a 10 team trade list)". Player
  pages carry no clause tooltip, only the per-season label in the Clause column.
- Player pages: one ``div.cf_playerContract`` per contract, under
  ``<h4>CURRENT CONTRACT</h4>`` or ``<h4>CONTRACT HISTORY</h4>``. "Value" is the
  sum of the AAV column, so on contracts with performance bonuses (ELCs) it
  includes the maximum bonuses (McDavid ELC: Value $11,325,000, cap hit $925,000).
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from .model import Claim, ClauseClaim, DeadCapClaim, SeasonClaim, Source, claim_id, make_key

PUBLISHER = "CapFriendly (Internet Archive snapshot)"
SOURCE_KIND = "wayback_capfriendly"
TIER = "E"
CF_BASE = "https://www.capfriendly.com"

# CapFriendly team slug -> NHL tri-code. The Coyotes page became Utah's in 2024.
TEAM_SLUGS: dict[str, str] = {
    "avalanche": "COL", "blackhawks": "CHI", "bluejackets": "CBJ", "blues": "STL", "bruins": "BOS",
    "canadiens": "MTL", "canucks": "VAN", "capitals": "WSH", "coyotes": "UTA", "devils": "NJD",
    "ducks": "ANA", "flames": "CGY", "flyers": "PHI", "goldenknights": "VGK", "hurricanes": "CAR",
    "islanders": "NYI", "jets": "WPG", "kings": "LAK", "kraken": "SEA", "lightning": "TBL",
    "mapleleafs": "TOR", "oilers": "EDM", "panthers": "FLA", "penguins": "PIT", "predators": "NSH",
    "rangers": "NYR", "redwings": "DET", "sabres": "BUF", "senators": "OTT", "sharks": "SJS",
    "stars": "DAL", "utah": "UTA", "wild": "MIN",
}

TEAM_NAMES: dict[str, str] = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL",
    "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL", "Detroit Red Wings": "DET",
    "Edmonton Oilers": "EDM", "Florida Panthers": "FLA", "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN",
    "Montreal Canadiens": "MTL", "Montréal Canadiens": "MTL", "Nashville Predators": "NSH",
    "New Jersey Devils": "NJD", "New York Islanders": "NYI", "New York Rangers": "NYR",
    "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT",
    "San Jose Sharks": "SJS", "Seattle Kraken": "SEA", "St. Louis Blues": "STL", "St Louis Blues": "STL",
    "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR", "Utah Hockey Club": "UTA",
    "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK", "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
}
# Signing teams that no longer exist under that name: mapped to the franchise's
# current code and flagged ``franchise_relocated``.
RELOCATED_TEAM_NAMES: dict[str, str] = {
    "Arizona Coyotes": "UTA", "Phoenix Coyotes": "UTA", "Atlanta Thrashers": "WPG",
}

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
SEASON_RE = re.compile(r"(20\d\d-\d\d)\s*$")
CLAUSE_KINDS = ("NMC", "NTC", "M-NTC", "M-NMC")
LIST_SIZE_RE = re.compile(r"(\d+)[\s-]+team", re.I)

# --------------------------------------------------------------------------
# A minimal, forgiving HTML tree. CapFriendly closes header cells with </td>
# ("<th>Cap%</td>"), so td/th close each other and table-internal end tags
# never reach outside their table.

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
CELL = {"td", "th"}
TABLE_PARTS = {"tr", "thead", "tbody", "tfoot"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Iterable[tuple[str, str | None]], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = {k: (v or "") for k, v in attrs}
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def text(self, block_sep: str = "") -> str:
        """Text content, whitespace collapsed. ``block_sep`` is put around div/p/br boundaries."""
        out: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if isinstance(c, str):
                    out.append(c)
                elif c.tag not in ("script", "style"):
                    if block_sep and c.tag in ("div", "p", "br", "li"):
                        out.append(block_sep)
                    walk(c)
                    if block_sep and c.tag in ("div", "p", "li"):
                        out.append(block_sep)

        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def iter(self, tag: str | None = None) -> Iterator[Node]:
        """Descendants in document order."""
        for c in self.children:
            if isinstance(c, Node):
                if tag is None or c.tag == tag:
                    yield c
                yield from c.iter(tag)

    def find_class(self, cls: str, tag: str | None = None) -> Iterator[Node]:
        return (n for n in self.iter(tag) if cls in n.classes)

    def first_class(self, cls: str, tag: str | None = None) -> Node | None:
        return next(self.find_class(cls, tag), None)

    def cells(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node) and c.tag in CELL]

    def ancestors(self) -> Iterator[Node]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", (), None)
        self.stack = [self.root]

    def _close(self, tags: set[str], stop: set[str]) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t in tags:
                del self.stack[i:]
                return
            if t in stop:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in CELL:
            self._close(CELL, {"tr", "table"})
        elif tag == "tr":
            self._close({"tr"}, {"table"})
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if tag in CELL:
            self._close(CELL, {"tr", "table"})
        elif tag in TABLE_PARTS:
            self._close({tag}, {"table"})
        else:
            self._close({tag}, set())

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


# --------------------------------------------------------------------------
# value helpers


def money(text: str | None) -> int | None:
    """"$12,500,000" -> 12500000; "-$2,454,167" -> -2454167; "-", "" -> None."""
    t = (text or "").strip()
    m = re.fullmatch(r"(-)?\$\s?([\d,]+)", t)
    if not m:
        return None
    value = int(m.group(2).replace(",", ""))
    return -value if m.group(1) else value


def parse_date(text: str | None) -> str | None:
    """"Jul. 5, 2017" / "Sept. 1, 2020" / "June 30, 2024" -> ISO date."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", text or "")
    if not m or m.group(1).lower() not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def ts_to_iso(ts: str) -> str:
    return datetime.strptime(ts[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ts_to_date(ts: str) -> str:
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def path_of(url: str) -> str:
    """Path of a CapFriendly URL, also from a Wayback URL (…/web/<ts>/https://www.capfriendly.com/x)."""
    m = re.search(r"capfriendly\.com(/[^?#]*)", url or "")
    path = m.group(1) if m else urlparse(url or "").path
    return path.rstrip("/") or "/"


def snapshot_ts_of(url: str) -> str | None:
    m = re.search(r"/web/(\d{14})", url or "")
    return m.group(1) if m else None


def team_code(name: str | None) -> tuple[str | None, bool]:
    """(tri-code, relocated?) for a CapFriendly full team name."""
    n = (name or "").strip()
    if n in TEAM_NAMES:
        return TEAM_NAMES[n], False
    if n in RELOCATED_TEAM_NAMES:
        return RELOCATED_TEAM_NAMES[n], True
    return None, False


def flip_name(name: str) -> str:
    """"McDavid, Connor" -> "Connor McDavid"."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def clause_tokens(label: str | None) -> list[str]:
    return [t.strip() for t in (label or "").split(",") if t.strip() in CLAUSE_KINDS]


def list_sizes(text: str | None) -> list[int]:
    return sorted({int(x) for x in LIST_SIZE_RE.findall(text or "")})


def list_type(text: str | None) -> str | None:
    t = (text or "").lower()
    if re.search(r"no[\s-]trade list", t):
        return "no-trade"
    return None


def _walk_sections(root: Node, heading_tag: str) -> Iterator[tuple[str, Node]]:
    """Yield (last heading text, node) for every element, in document order."""
    heading = ""
    for n in root.iter():
        if n.tag == heading_tag:
            heading = n.text()
        yield heading, n


# --------------------------------------------------------------------------
# team pages

SPAN_FIELDS = {"cap": "cap_hit", "aav": "aav", "sal": "total_salary", "bsal": "base_salary",
               "pbonus": "performance_bonus", "sbonus": "signing_bonus"}


def _season_cell(cell: Node, season: str) -> dict[str, Any] | None:
    spans = [s for s in cell.iter("span") if set(s.classes) & (set(SPAN_FIELDS) | {"cls"})]
    if not spans:
        return None
    out: dict[str, Any] = {"season": season, "cap_hit": None, "aav": None, "total_salary": None,
                           "base_salary": None, "performance_bonus": None, "signing_bonus": None,
                           "clause": None, "two_way": False}
    for s in spans:
        cls = set(s.classes)
        if "twoway" in cls:
            out["two_way"] = True
        for key, field in SPAN_FIELDS.items():
            if key in cls:
                out[field] = money(s.text())
        if "cls" in cls:
            out["clause"] = s.text() or None
    if all(out[f] is None for f in SPAN_FIELDS.values()):
        return None
    return out


def _team_tables(root: Node) -> Iterator[tuple[str, str, Node]]:
    """(section, group header, table) for the roster-style tables."""
    for section, n in _walk_sections(root, "h3"):
        if n.tag != "table" or "cf_teamProfileRosterSection__table" not in n.classes:
            continue
        head = next(n.iter("tr"), None)
        first = head.cells()[0].text() if head and head.cells() else ""
        yield section.upper(), first, n


def _header(table: Node) -> list[str]:
    head = next(table.iter("tr"))
    return [c.text() for c in head.cells()]


def _body_rows(table: Node) -> Iterator[Node]:
    for tr in table.iter("tr"):
        if any(a.tag in ("thead", "tfoot") for a in tr.ancestors() if a is not table):
            continue
        cells = tr.cells()
        if not cells or cells[0].tag == "th":
            continue
        if cells[0].text().upper().startswith("TOTAL"):
            continue
        yield tr


def _page_team(root: Node, snapshot_url: str) -> tuple[str | None, str | None]:
    m = re.search(r"/teams/([a-z]+)", path_of(snapshot_url))
    slug = m.group(1) if m else None
    return slug, TEAM_SLUGS.get(slug or "")


def parse_team_page(html: str, snapshot_url: str, snapshot_ts: str) -> list[dict[str, Any]]:
    """One dict per player row in the Forwards/Defense/Goalies tables (NHL roster and the
    non-roster minor/junior tables). TOTAL rows and dead-cap tables are skipped."""
    root = parse_html(html)
    slug, code = _page_team(root, snapshot_url)
    rows: list[dict[str, Any]] = []
    for section, group, table in _team_tables(root):
        gm = re.match(r"(Forwards|Defense|Defence|Goalies)\b", group)
        if not gm:
            continue
        header = _header(table)
        idx = {h: i for i, h in enumerate(header)}
        season_cols = [(i, SEASON_RE.search(h).group(1)) for i, h in enumerate(header) if SEASON_RE.search(h)]
        for tr in _body_rows(table):
            cells = tr.cells()
            if len(cells) < len(header):
                continue
            rows.append(_team_row(cells, idx, season_cols, section, gm.group(1), slug, code,
                                  snapshot_url, snapshot_ts))
    return rows


def _cell(cells: list[Node], idx: dict[str, int], name: str) -> Node | None:
    i = idx.get(name)
    return cells[i] if i is not None and i < len(cells) else None


def _team_row(cells: list[Node], idx: dict[str, int], season_cols: list[tuple[int, str]], section: str,
              group: str, slug: str | None, code: str | None, snapshot_url: str, snapshot_ts: str
              ) -> dict[str, Any]:
    first = cells[0]
    link = next((a for a in first.iter("a") if "/players/" in a.attrs.get("href", "")), None)
    name_raw = link.text() if link else first.text()
    captaincy = re.search(r"\"([CA])\"", first.text())
    yr = _cell(cells, idx, "Years Remaining")
    yr_text = yr.text() if yr else ""
    ym = re.match(r"(\d+)\s*(.*)", yr_text)
    terms_cell = _cell(cells, idx, "Terms")
    terms = terms_cell.text() if terms_cell else ""
    term_notes: list[str] = []
    clause_detail = None
    flags: list[str] = []
    if terms_cell:
        for s in terms_cell.iter("span"):
            if "q" not in s.classes or not s.attrs.get("title"):
                continue
            title = s.attrs["title"]
            label = s.text()
            if clause_tokens(label):
                clause_detail = title
            elif label:
                term_notes.append(f"{label}: {title}")
            else:
                term_notes.append(title)
            if title.startswith("The contract details displayed have not yet been confirmed"):
                flags.append("unconfirmed")
    acquired = _cell(cells, idx, "Acquired")
    acq_q = acquired.first_class("q", "span") if acquired else None
    age = _cell(cells, idx, "Age")
    age_q = age.first_class("q", "span") if age else None
    cap_pct = _cell(cells, idx, "Cap%")
    seasons: list[dict[str, Any]] = []
    expiry_season = None
    for i, season in season_cols:
        cell = cells[i]
        sc = _season_cell(cell, season)
        if sc:
            seasons.append(sc)
        elif expiry_season is None and (cell.text() in ("UFA", "RFA") or cell.text() == "10.2(c)"):
            expiry_season = season
    href = link.attrs["href"] if link else None
    return {
        "name": flip_name(name_raw),
        "name_raw": name_raw,
        "captaincy": captaincy.group(1) if captaincy else None,
        "player_path": path_of(href) if href else None,
        "years_remaining": int(ym.group(1)) if ym else None,
        "expiry_status": (ym.group(2).strip() or None) if ym else None,
        "terms": terms,
        "clause_detail": clause_detail,
        "term_notes": term_notes,
        "position": (_cell(cells, idx, "POS").text() if _cell(cells, idx, "POS") else None),
        "status": (_cell(cells, idx, "Status").text() if _cell(cells, idx, "Status") else None),
        "acquired": acquired.text() if acquired else None,
        "acquired_detail": acq_q.attrs.get("title") if acq_q else None,
        "age": int(age.text()) if age and age.text().isdigit() else None,
        "birth_date": parse_date(age_q.attrs.get("title")) if age_q else None,
        "cap_pct": cap_pct.text() if cap_pct and cap_pct.text() not in ("", "-") else None,
        "group": group,
        "section": section or "ROSTER",
        "team_slug": slug,
        "team": code,
        "seasons": seasons,
        "expiry_season": expiry_season,
        "flags": flags,
        "snapshot_url": snapshot_url,
        "snapshot_ts": snapshot_ts,
    }


DEAD_CAP_GROUPS = (("Buyout History", "buyout"), ("Retained Salary", "retained"))


def parse_team_dead_cap(html: str, snapshot_url: str, snapshot_ts: str) -> list[dict[str, Any]]:
    """Rows of the DEAD CAP tables: "Buyout History" (``kind`` buyout) and "Retained
    Salary" (``kind`` retained), one per player with the charge per remaining season.
    Each season dict: season, cap_hit, aav, total_salary, signing_bonus (as written).
    The retained table does not name the acquiring team."""
    root = parse_html(html)
    slug, code = _page_team(root, snapshot_url)
    out: list[dict[str, Any]] = []
    for _section, group, table in _team_tables(root):
        kind = next((k for prefix, k in DEAD_CAP_GROUPS if group.startswith(prefix)), None)
        if kind is None:
            continue
        header = _header(table)
        season_cols = [(i, SEASON_RE.search(h).group(1)) for i, h in enumerate(header) if SEASON_RE.search(h)]
        for tr in _body_rows(table):
            cells = tr.cells()
            link = next((a for a in cells[0].iter("a") if "/players/" in a.attrs.get("href", "")), None)
            name_raw = link.text() if link else cells[0].text()
            seasons = []
            for i, season in season_cols:
                if i >= len(cells):
                    continue
                sc = _season_cell(cells[i], season)
                if sc:
                    seasons.append({k: sc[k] for k in ("season", "cap_hit", "aav", "total_salary", "signing_bonus")})
            out.append({
                "name": flip_name(name_raw), "name_raw": name_raw, "kind": kind, "group": group,
                "player_path": path_of(link.attrs["href"]) if link else None,
                "team_slug": slug, "team": code, "seasons": seasons,
                "snapshot_url": snapshot_url, "snapshot_ts": snapshot_ts,
            })
    return out


def parse_team_buyouts(html: str, snapshot_url: str, snapshot_ts: str) -> list[dict[str, Any]]:
    """The buyout rows of :func:`parse_team_dead_cap` (kept for callers of the old name)."""
    return [r for r in parse_team_dead_cap(html, snapshot_url, snapshot_ts) if r["kind"] == "buyout"]


def dead_cap_claims(rows: list[dict[str, Any]]) -> tuple[list[DeadCapClaim], list[Source]]:
    """Dead-cap table rows (newest snapshot per team only) -> tier E claims, one per
    (player, season), and the page sources."""
    newest: dict[str, str] = {}
    for r in rows:
        if r["snapshot_ts"] > newest.get(r["team_slug"], ""):
            newest[r["team_slug"]] = r["snapshot_ts"]
    claims: list[DeadCapClaim] = []
    sources: dict[str, Source] = {}
    for r in rows:
        if r["snapshot_ts"] != newest[r["team_slug"]]:
            continue
        src = _source(r["snapshot_url"], r["snapshot_ts"], f"CapFriendly team page: {r['team_slug']}")
        sources.setdefault(src.source_id, src)
        for sc in r["seasons"]:
            if sc.get("cap_hit") is None:
                continue
            claims.append(DeadCapClaim(
                claim_id=claim_id(f"{src.source_id}#{r['kind']}:{sc['season']}", r["name"], None),
                kind=r["kind"], player_name=r["name"], player_id=None, team=r.get("team"), counterparty=None,
                season=sc["season"], cap_hit_usd=sc["cap_hit"], pct=None, contract_key=None, event_date=None,
                source_id=src.source_id, confidence_tier=TIER, flags=["team_page_dead_cap"],
                evidence=f"team page {r['group']}: {r['name_raw']} | {sc['season']} ${sc['cap_hit']:,}",
            ))
    return claims, list(sources.values())


def dead_cap_rows_from_store(store: Any) -> tuple[list[DeadCapClaim], list[Source]]:
    """Every stored ``ok`` team page -> dead-cap claims (buyouts and retained salary)."""
    rows: list[dict[str, Any]] = []
    for p in store.wayback_pages(kind="team"):
        if "/teams/coyotes" in p["url"]:
            continue
        rows.extend(parse_team_dead_cap(gzip.decompress(p["html"]).decode("utf-8", "replace"),
                                        p["snapshot_url"], p["snapshot_ts"]))
    return dead_cap_claims(rows)


def team_page_player_paths(html: str) -> list[str]:
    """Every /players/<slug> path linked from the roster, non-roster and buyout tables."""
    root = parse_html(html)
    paths: set[str] = set()
    for _section, _group, table in _team_tables(root):
        for a in table.iter("a"):
            href = a.attrs.get("href", "")
            if re.fullmatch(r"(?:https?://(?:www\.)?capfriendly\.com)?/players/[a-z0-9-]+/?", href):
                paths.add(path_of(href))
    return sorted(paths)


# --------------------------------------------------------------------------
# player pages

PLAYER_COLUMNS = {
    "SEASON": "season", "Clause": "clause", "Cap Hit": "cap_hit", "AAV": "aav",
    "P. Bonuses": "performance_bonus", "S. Bonuses": "signing_bonus", "Base Salary": "base_salary",
    "Total Salary": "total_salary", "Minors Sal": "minors_salary",
}
MONEY_COLUMNS = {"cap_hit", "aav", "performance_bonus", "signing_bonus", "base_salary", "total_salary",
                 "minors_salary"}


def contract_type_of(title: str) -> tuple[str, list[str]]:
    t = title.lower()
    flags: list[str] = []
    if "35+" in t:
        flags.append("35_plus")
    if "entry-level" in t or "entry level" in t:
        return "ELC", flags
    if "tryout" in t:
        return "PTO", flags
    if "extension" in t:
        return "extension", flags
    if "standard" in t or "35+" in t:
        return "standard", flags
    return "standard", [*flags, "unrecognized_contract_type"]


def _meta(block: Node) -> dict[str, tuple[str, Node]]:
    out: dict[str, tuple[str, Node]] = {}
    for span in block.find_class("cf_playerContract__meta_title", "span"):
        label = span.text()
        holder = span.parent
        if holder is None:
            continue
        value = holder.text()
        value = value[len(label):] if value.startswith(label) else value
        out[label] = (value.lstrip(" :").strip(), holder)
    return out


def _contract(block: Node, section: str) -> dict[str, Any]:
    title_node = block.first_class("cf_playerContract__title")
    title = title_node.text() if title_node else ""
    ctype, flags = contract_type_of(title)
    signed_by = None
    for div in block.iter("div"):
        if div.text().startswith("SIGNED BY:") and not any(isinstance(c, Node) and c.tag == "div"
                                                          for c in div.children):
            signed_by = div.text().split(":", 1)[1].strip() or None
            break
    meta = _meta(block)
    length = meta.get("Length", ("", None))[0]
    lm = re.match(r"(\d+)\s*year", length)
    team_name = meta.get("Signing Team", ("", None))[0]
    team_link = None
    if "Signing Team" in meta:
        team_link = next((a.attrs.get("href") for a in meta["Signing Team"][1].iter("a")), None)
    code, relocated = team_code(team_name)
    if relocated:
        flags.append("franchise_relocated")
    if team_name and not code:
        flags.append("unknown_team")
    source_text, source_holder = meta.get("Source", ("", None))
    source_url = next((a.attrs.get("href") for a in source_holder.iter("a")), None) if source_holder else None
    desc = block.first_class("cf_playerContract__description")
    notes = block.first_class("cf_playerContract__notes")
    clause_source = None
    if notes:
        m = re.search(r"CLAUSE SOURCE\s*:\s*(.+)", notes.text())
        clause_source = m.group(1).strip() if m else None
    seasons, totals = _contract_table(block.first_class("cf_table__playerContract", "table"))
    return {
        "section": section,
        "title": title,
        "contract_type": ctype,
        "flags": flags,
        "signed_by": signed_by,
        "length_years": int(lm.group(1)) if lm else None,
        "value_usd": money(meta.get("Value", ("", None))[0]),
        "expiry_status": meta.get("Expiry Status", ("", None))[0] or None,
        "cap_pct": meta.get("Cap %", ("", None))[0] or None,
        "signing_team_name": team_name or None,
        "signing_team": code,
        "signing_team_slug": path_of(team_link).rsplit("/", 1)[-1] if team_link else None,
        "signing_date": parse_date(meta.get("Signing Date", ("", None))[0]),
        "signing_date_raw": meta.get("Signing Date", ("", None))[0] or None,
        "source": source_text or None,
        "source_url": source_url,
        "summary": desc.text() if desc else "",
        "clause_source": clause_source,
        "notes": notes.text() if notes else None,
        "meta": {k: v for k, (v, _n) in meta.items()},
        "seasons": seasons,
        "totals": totals,
    }


def _contract_table(table: Node | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if table is None:
        return [], None
    header = _header(table)
    cols = [PLAYER_COLUMNS.get(h, h) for h in header]
    seasons: list[dict[str, Any]] = []
    totals = None
    for tr in table.iter("tr"):
        cells = tr.cells()
        if not cells or cells[0].tag == "th":
            continue
        row: dict[str, Any] = {}
        for col, cell in zip(cols, cells, strict=False):
            text = cell.text()
            row[col] = money(text) if col in MONEY_COLUMNS else (text or None)
            if col == "clause":
                detail = [n.attrs["title"] for n in cell.iter() if n.attrs.get("title")]
                row["clause_detail"] = "; ".join(detail) or None
        if (row.get("season") or "").upper() == "TOTAL":
            totals = row
        elif row.get("season") and SEASON_RE.search(row["season"]):
            row["raw"] = " | ".join(c.text() for c in cells)
            seasons.append(row)
    return seasons, totals


def parse_player_page(html: str, snapshot_url: str, snapshot_ts: str) -> dict[str, Any]:
    root = parse_html(html)
    title = next(root.iter("title"), None)
    name = None
    if title:
        m = re.match(r"(.+?) Contract, ", title.text())
        name = m.group(1).strip() if m else None
    h1 = next(root.iter("h1"), None)
    number = None
    if h1:
        nm = re.search(r"#(\d+)", h1.text())
        number = int(nm.group(1)) if nm else None
        if not name:
            name = re.sub(r"\s*#\d+\s*$", "", h1.text()).title()
    bio: dict[str, str] = {}
    for cls in ("pld_l", "pld_r"):
        for span in root.find_class(cls, "span"):
            label = span.text().rstrip(": ").strip()
            holder = span.parent
            if holder is None or not label:
                continue
            value = holder.text(block_sep=" ")
            bio[label] = value[len(span.text()):].strip() if value.startswith(span.text()) else value
    current_team = None
    h3 = next(root.iter("h3"), None)
    if h3:
        a = next((a for a in h3.iter("a") if "/teams/" in a.attrs.get("href", "")), None)
        if a:
            current_team = TEAM_SLUGS.get(path_of(a.attrs["href"]).rsplit("/", 1)[-1])
    contracts = []
    for section, n in _walk_sections(root, "h4"):
        if n.tag == "div" and n.classes == ["cf_playerContract"]:
            contracts.append(_contract(n, section))

    def as_int(label: str) -> int | None:
        v = bio.get(label, "")
        return int(v) if v.isdigit() else None

    draft = None
    if "DRAFT YEAR" in bio:
        draft = {"year": as_int("DRAFT YEAR"), "round": as_int("DRAFT ROUND"),
                 "overall": as_int("DRAFTED OVERALL"), "team": bio.get("DRAFTED BY") or None}
    return {
        "name": name,
        "player_path": path_of(snapshot_url),
        "number": number,
        "current_team": current_team,
        "birth_date": parse_date(bio.get("BORN")),
        "bio": bio,
        "draft": draft,
        "contracts": contracts,
        "snapshot_url": snapshot_url,
        "snapshot_ts": snapshot_ts,
    }


def parse_browse_page(html: str) -> list[dict[str, Any]]:
    """Rows of a /browse/active listing page (50 players per page)."""
    root = parse_html(html)
    out = []
    table = next((t for t in root.iter("table") if t.attrs.get("id") == "brwt"), None)
    if table is None:
        return out
    header = _header(table)
    for tr in _body_rows(table):
        cells = tr.cells()
        if len(cells) != len(header):
            continue
        row = {h: c.text() for h, c in zip(header, cells, strict=True)}
        link = next((a for a in cells[0].iter("a") if "/players/" in a.attrs.get("href", "")), None)
        out.append({
            "name": re.sub(r"^\d+\.\s*", "", row.get("PLAYER", "")),
            "player_path": path_of(link.attrs["href"]) if link else None,
            "team": row.get("TEAM") or None,
            "clause": row.get("CLAUSE") if row.get("CLAUSE") not in ("", "-") else None,
            "expiry_status": row.get("EXPIRY") or None,
            "cap_hit": money(row.get("CAP HIT")),
            "salary": money(row.get("SALARY")),
        })
    return out


# --------------------------------------------------------------------------
# rows


def source_id_for(snapshot_ts: str, path: str) -> str:
    return f"wayback:{snapshot_ts}:{path}"


def snapshot_url_for(snapshot_ts: str, path: str) -> str:
    return f"https://web.archive.org/web/{snapshot_ts}/{CF_BASE}{path}"


def _source(snapshot_url: str, snapshot_ts: str, headline: str) -> Source:
    path = path_of(snapshot_url)
    return Source(
        source_id=source_id_for(snapshot_ts, path), url=snapshot_url, publisher=PUBLISHER, author=None,
        published_at=ts_to_iso(snapshot_ts), confidence_tier=TIER, headline=headline, kind=SOURCE_KIND,
    )


def _clause_runs(seasons: list[tuple[str, str | None]]) -> list[tuple[str, str, str]]:
    """[(season, label)] in order -> [(kind, season_from, season_to)] for consecutive runs."""
    runs: list[tuple[str, str, str]] = []
    open_runs: dict[str, list[str]] = {}
    for season, label in seasons:
        kinds = set(clause_tokens(label))
        for kind in list(open_runs):
            if kind not in kinds:
                span = open_runs.pop(kind)
                runs.append((kind, span[0], span[-1]))
        for kind in clause_tokens(label):
            open_runs.setdefault(kind, []).append(season)
    runs.extend((kind, span[0], span[-1]) for kind, span in open_runs.items())
    return sorted(runs, key=lambda r: (r[1], CLAUSE_KINDS.index(r[0])))


def _team_season_values(s: dict[str, Any]) -> dict[str, Any]:
    """Team-page season cell -> SeasonClaim money fields. Two-way cells show the minor-league
    salary in sal/bsal, so it goes to minors_salary and the NHL salary stays unknown."""
    if s["two_way"]:
        return {"cap_hit_usd": s["cap_hit"], "aav_usd": s["aav"], "base_salary_usd": None,
                "signing_bonus_usd": s["signing_bonus"], "performance_bonus_usd": s["performance_bonus"],
                "total_salary_usd": None, "minors_salary_usd": s["total_salary"], "one_way": False}
    return {"cap_hit_usd": s["cap_hit"], "aav_usd": s["aav"], "base_salary_usd": s["base_salary"],
            "signing_bonus_usd": s["signing_bonus"], "performance_bonus_usd": s["performance_bonus"],
            "total_salary_usd": s["total_salary"], "minors_salary_usd": None, "one_way": None}


def _season_text(s: dict[str, Any]) -> str:
    parts = [f"{k}={s[k]}" for k in ("cap_hit", "aav", "total_salary", "base_salary", "performance_bonus",
                                     "signing_bonus") if s.get(k) is not None]
    tag = " (two-way cell: salary shown is the minor-league salary)" if s.get("two_way") else ""
    return f"team page {s['season']}: {', '.join(parts)}" + (f", clause={s['clause']}" if s.get("clause") else "") + tag


def _player_season_values(r: dict[str, Any]) -> dict[str, Any]:
    minors, total = r.get("minors_salary"), r.get("total_salary")
    one_way = None
    if minors is not None and total is not None:
        one_way = minors == total
    return {"cap_hit_usd": r.get("cap_hit"), "aav_usd": r.get("aav"), "base_salary_usd": r.get("base_salary"),
            "signing_bonus_usd": r.get("signing_bonus"), "performance_bonus_usd": r.get("performance_bonus"),
            "total_salary_usd": total, "minors_salary_usd": minors, "one_way": one_way}


def _same_figures(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for k in ("cap_hit_usd", "aav_usd", "base_salary_usd", "signing_bonus_usd", "performance_bonus_usd",
              "total_salary_usd", "minors_salary_usd"):
        if a.get(k) is not None and b.get(k) is not None and a[k] != b[k]:
            return False
    return True


def to_rows(team_rows: list[dict[str, Any]], player_pages: list[dict[str, Any]],
            snapshot_meta: dict[str, dict[str, Any]] | None = None
            ) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], list[Source]]:
    """Turn parsed pages into claim rows.

    ``team_rows`` is the concatenation of ``parse_team_page`` outputs, ``player_pages`` a list of
    ``parse_player_page`` outputs. ``snapshot_meta`` optionally maps a snapshot URL to
    ``{"headline": ...}`` for the Source row; the parsed dicts already carry URL and timestamp.
    """
    meta = snapshot_meta or {}
    claims: list[Claim] = []
    seasons_out: list[SeasonClaim] = []
    clauses_out: list[ClauseClaim] = []
    sources: dict[str, Source] = {}

    def add_source(url: str, ts: str, headline: str) -> str:
        src = _source(url, ts, meta.get(url, {}).get("headline") or headline)
        sources.setdefault(src.source_id, src)
        return src.source_id

    # player path -> [(Claim, contract season dicts)]
    by_path: dict[str, list[tuple[Claim, dict[str, dict[str, Any]]]]] = {}
    for page in player_pages:
        if not page.get("contracts"):
            continue
        sid = add_source(page["snapshot_url"], page["snapshot_ts"], page.get("name") or page["player_path"])
        for i, c in enumerate(page["contracts"]):
            claim, srows, crows, season_map = _player_contract_rows(page, c, i, sid)
            claims.append(claim)
            seasons_out.extend(srows)
            clauses_out.extend(crows)
            by_path.setdefault(page["player_path"], []).append((claim, season_map))

    # team rows: group by player, newest snapshot first
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in team_rows:
        key = r.get("player_path") or f"name:{r['name']}"
        groups.setdefault(key, []).append(r)
    for key, rows in groups.items():
        rows.sort(key=lambda r: r["snapshot_ts"], reverse=True)
        emitted: list[list[dict[str, Any]]] = []
        for r in rows:
            if not r["seasons"]:
                continue
            values = [_team_season_values(s) for s in r["seasons"]]
            if any(len(e) == len(values) and all(_same_figures(a, b) for a, b in zip(e, values, strict=True))
                   for e in emitted):
                continue  # the same row on another team page (e.g. coyotes and utah)
            emitted.append(values)
            sid = add_source(r["snapshot_url"], r["snapshot_ts"], f"{r.get('team') or r.get('team_slug')} team page")
            covered = by_path.get(key, [])
            uncovered = []
            for s, v in zip(r["seasons"], values, strict=True):
                hit = next(((cl, sm[s["season"]]) for cl, sm in covered if s["season"] in sm), None)
                if hit is None:
                    uncovered.append(s)
                    continue
                cl, pv = hit
                if not _same_figures(v, pv):
                    seasons_out.append(SeasonClaim(
                        claim_id=cl.claim_id, contract_key=cl.contract_key, season=s["season"], clause=s["clause"],
                        source_id=sid, confidence_tier=TIER, evidence=_season_text(s), **v))
            if covered:
                clauses_out.extend(_team_clause_enrichment(r, covered, sid))
            segments = _split_by_cap_hit(uncovered)
            for seg in segments:
                tc, ts_rows, tcl = _team_only_rows(r, seg, sid, partial=bool(covered))
                if len(segments) > 1:
                    tc.flags.append("split_by_cap_hit")
                claims.append(tc)
                seasons_out.extend(ts_rows)
                clauses_out.extend(tcl)
    return claims, seasons_out, clauses_out, list(sources.values())


def _split_by_cap_hit(seasons: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A team page lists a player's current deal and a signed extension in one row.
    A cap hit is constant within one contract, so a change in cap hit between
    consecutive seasons starts a new contract segment."""
    segments: list[list[dict[str, Any]]] = []
    for s in seasons:
        if segments and segments[-1][-1]["cap_hit"] == s["cap_hit"]:
            segments[-1].append(s)
        else:
            segments.append([s])
    return segments


def _player_contract_rows(page: dict[str, Any], c: dict[str, Any], i: int, sid: str
                          ) -> tuple[Claim, list[SeasonClaim], list[ClauseClaim], dict[str, dict[str, Any]]]:
    name = page.get("name") or ""
    flags = list(c["flags"])
    signed_at = c["signing_date"]
    stated = signed_at is not None
    if not stated:
        signed_at = ts_to_date(page["snapshot_ts"])
        flags.append("signing_date_unknown")
    rows = c["seasons"]
    caps = {r.get("cap_hit") for r in rows if r.get("cap_hit") is not None}
    aavs = {r.get("aav") for r in rows if r.get("aav") is not None}
    if len(caps) > 1:
        flags.append("varying_cap_hit")
    if len(aavs) > 1:
        flags.append("varying_aav")
    if c["length_years"] is not None and rows and len(rows) != c["length_years"]:
        flags.append("season_span_mismatch")
    if any((r.get("performance_bonus") or 0) > 0 for r in rows):
        flags.append("performance_bonuses")
    value = c["value_usd"]
    if value is not None and rows:
        sum_cap = sum(r.get("cap_hit") or 0 for r in rows)
        sum_aav = sum(r.get("aav") or 0 for r in rows)
        if value == sum_aav and sum_aav != sum_cap:
            flags.append("total_includes_performance_bonuses")
        elif value not in (sum_cap, sum_aav):
            flags.append("total_vs_seasons_mismatch")
    season_vals = {r["season"]: _player_season_values(r) for r in rows}
    one_way_set = {v["one_way"] for v in season_vals.values() if v["one_way"] is not None}
    one_way = one_way_set.pop() if len(one_way_set) == 1 else None
    if one_way is not None:
        flags.append("derived:one_way_from_minors_salary")
    if not c["signing_team"] and "unknown_team" not in flags:
        flags.append("unknown_team")
    cid = claim_id(f"{sid}|{signed_at}|{i}", name, None)
    key = make_key(None, name, c["signing_team"], signed_at)
    first = rows[0] if rows else {}
    claim = Claim(
        claim_id=cid, contract_key=key, player_name=name, player_id=None, team=c["signing_team"],
        signed_at=signed_at, contract_type=c["contract_type"], term_years=c["length_years"],
        aav_usd=first.get("aav"), cap_hit_usd=first.get("cap_hit"), total_usd=value,
        nhl_salary_usd=None, minors_salary_usd=None, one_way=one_way,
        start_season=rows[0]["season"] if rows else None, end_season=rows[-1]["season"] if rows else None,
        re_signed=c["contract_type"] == "extension", source_id=sid,
        source_url=page["snapshot_url"], publisher=PUBLISHER, published_at=ts_to_iso(page["snapshot_ts"]),
        confidence_tier=TIER, extracted_by="regex", flags=flags, evidence=c["summary"],
        expiry_status=c["expiry_status"], agent=None, signing_date_stated=stated,
    )
    srows = [SeasonClaim(claim_id=cid, contract_key=key, season=r["season"], clause=r.get("clause"),
                         source_id=sid, confidence_tier=TIER, evidence=r.get("raw", ""), **season_vals[r["season"]])
             for r in rows]
    crows = []
    for kind, s_from, s_to in _clause_runs([(r["season"], r.get("clause")) for r in rows]):
        detail = next((r.get("clause_detail") for r in rows if r.get("clause_detail")
                       and kind in clause_tokens(r.get("clause"))), None)
        sizes = list_sizes(detail)
        ev = f"{c['title']}: Clause column {kind} {s_from}..{s_to}"
        if detail:
            ev += f"; {detail}"
        if c.get("clause_source"):
            ev += f"; clause source: {c['clause_source']}"
        crows.append(ClauseClaim(claim_id=cid, contract_key=key, kind=kind, season_from=s_from, season_to=s_to,
                                 list_size=sizes[0] if len(sizes) == 1 and kind != "NMC" else None,
                                 list_type=list_type(detail), source_id=sid, confidence_tier=TIER, evidence=ev))
    return claim, srows, crows, season_vals


def _team_clause_claims(r: dict[str, Any], seasons: list[dict[str, Any]], cid: str, key: str, sid: str
                        ) -> list[ClauseClaim]:
    detail = r.get("clause_detail")
    sizes = list_sizes(detail)
    out = []
    for kind, s_from, s_to in _clause_runs([(s["season"], s["clause"]) for s in seasons]):
        ev = f"team page Terms {r['terms']!r}, season clause {kind} {s_from}..{s_to}"
        if detail:
            ev += f"; tooltip: {detail}"
        out.append(ClauseClaim(
            claim_id=cid, contract_key=key, kind=kind, season_from=s_from, season_to=s_to,
            # a single stated list size belongs to the modified clause, never to an NMC run
            list_size=sizes[0] if len(sizes) == 1 and kind in ("M-NTC", "NTC", "M-NMC") else None,
            list_type=list_type(detail) if kind != "NMC" else None,
            source_id=sid, confidence_tier=TIER, evidence=ev))
    return out


def _team_clause_enrichment(r: dict[str, Any], covered: list[tuple[Claim, dict[str, dict[str, Any]]]],
                            sid: str) -> list[ClauseClaim]:
    """Team-page clause runs that add a list size the player page does not state, linked to the
    player-page contract that covers those seasons (source_id stays the team page)."""
    if not r.get("clause_detail"):
        return []
    out = []
    for cl, smap in covered:
        seasons = [s for s in r["seasons"] if s["season"] in smap]
        for cc in _team_clause_claims(r, seasons, cl.claim_id, cl.contract_key, sid):
            if cc.list_size is not None:
                out.append(cc)
    return out


def _team_only_rows(r: dict[str, Any], seasons: list[dict[str, Any]], sid: str, *, partial: bool
                    ) -> tuple[Claim, list[SeasonClaim], list[ClauseClaim]]:
    name = r["name"]
    signed_at = ts_to_date(r["snapshot_ts"])
    terms = r.get("terms") or ""
    flags = ["signing_date_unknown", "term_is_remaining_seasons", *r.get("flags", [])]
    if partial:
        flags.append("not_on_player_page")
    term_tokens = {t.strip() for t in terms.split(",")}
    ctype = "ELC" if "ELC" in term_tokens else "PTO" if "PTO" in term_tokens else "standard"
    if "35+" in term_tokens:
        flags.append("35_plus")
    if "RS" in term_tokens:
        flags.append("retained_salary_adjusted")
    if any(s["two_way"] for s in seasons):
        flags.append("two_way_minors_salary_shown")
    if len({s["cap_hit"] for s in seasons if s["cap_hit"] is not None}) > 1:
        flags.append("varying_cap_hit")
    if len({s["aav"] for s in seasons if s["aav"] is not None}) > 1:
        flags.append("varying_aav")
    if any((s["performance_bonus"] or 0) > 0 for s in seasons):
        flags.append("performance_bonuses")
    cid = claim_id(sid, name, None)
    key = make_key(None, name, r.get("team"), signed_at)
    ev = f"team page row: {r['name_raw']} | {r['years_remaining']} {r['expiry_status'] or ''} | {terms} | " \
         f"{r.get('position') or ''} | {r.get('status') or ''}"
    if r.get("clause_detail"):
        ev += f" | {r['clause_detail']}"
    claim = Claim(
        claim_id=cid, contract_key=key, player_name=name, player_id=None, team=r.get("team"), signed_at=signed_at,
        contract_type=ctype, term_years=len(seasons), aav_usd=seasons[0]["aav"], cap_hit_usd=seasons[0]["cap_hit"],
        total_usd=None, nhl_salary_usd=None, minors_salary_usd=None,
        one_way=False if any(s["two_way"] for s in seasons) else None,
        start_season=seasons[0]["season"], end_season=seasons[-1]["season"], re_signed=False, source_id=sid,
        source_url=r["snapshot_url"], publisher=PUBLISHER, published_at=ts_to_iso(r["snapshot_ts"]),
        confidence_tier=TIER, extracted_by="regex", flags=flags, evidence=ev,
        expiry_status=r.get("expiry_status"), agent=None, signing_date_stated=False,
    )
    srows = [SeasonClaim(claim_id=cid, contract_key=key, season=s["season"], clause=s["clause"], source_id=sid,
                         confidence_tier=TIER, evidence=_season_text(s), **_team_season_values(s)) for s in seasons]
    return claim, srows, _team_clause_claims(r, seasons, cid, key, sid)


def rows_from_store(store: Any) -> tuple[list[Claim], list[SeasonClaim], list[ClauseClaim], list[Source]]:
    """Parse every stored ``ok`` team and player page and build rows (for the integrator)."""
    team_rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for p in store.wayback_pages(kind="team"):
        if "/teams/coyotes" in p["url"]:
            continue  # the Arizona page froze at 2023-24; the utah page carries the franchise
        team_rows.extend(parse_team_page(gzip.decompress(p["html"]).decode("utf-8", "replace"),
                                         p["snapshot_url"], p["snapshot_ts"]))
    for p in store.wayback_pages(kind="player"):
        pages.append(parse_player_page(gzip.decompress(p["html"]).decode("utf-8", "replace"),
                                       p["snapshot_url"], p["snapshot_ts"]))
    return to_rows(team_rows, pages)
