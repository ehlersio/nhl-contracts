"""Endpoints, paths and constants shared by the collectors."""

from __future__ import annotations

import os
from pathlib import Path

FORGE_BASE = "https://forge-dapi.d3.nhle.com/v2/content/en-us"
FORGE_STORIES = f"{FORGE_BASE}/stories"
NHL_TEAMS = "https://api.nhle.com/stats/rest/en/team"
NHL_STANDINGS = "https://api-web.nhle.com/v1/standings/now"
ESPN_TRANSACTIONS = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/transactions"

# Every Forge list is capped at 25 items and ignores paging, so the collector
# polls several narrow lists per team. The plain ``teamid-N`` list catches club
# releases and NHL.com staff stories that carry neither ``transactions`` nor
# ``press-release`` (Boston's staff signing stories, for example).
FORGE_TEAM_LISTS = ("transactions,teamid-{id}", "press-release,teamid-{id}", "teamid-{id}")
FORGE_LEAGUE_LISTS = ("transactions",)

USER_AGENT = (
    "nhl-contracts/0.1 (+https://github.com/mattehlers/nhl-contracts; open dataset collector)"
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NHL_CONTRACTS_DATA", ROOT / "data"))
DIST_DIR = Path(os.environ.get("NHL_CONTRACTS_DIST", ROOT / "dist"))
STORE_PATH = DATA_DIR / "raw.sqlite"          # working store, rebuilt from the two copies below
RAW_JSONL_DIR = DATA_DIR / "raw"               # committed to the repository
WAYBACK_STORE = DATA_DIR / "wayback_pages.sqlite"  # release asset, one-off archive crawl
# Hand-contributed claims are part of the repository, so they never move with NHL_CONTRACTS_DATA.
CONTRIBUTED_CLAIMS = ROOT / "data" / "contributed" / "claims.csv"

# Static fallback for offline use and tests; the live map is fetched and cached
# by ``nhl_contracts.teams``. NHL team ids are stable.
STATIC_TEAMS: dict[int, str] = {
    1: "NJD", 2: "NYI", 3: "NYR", 4: "PHI", 5: "PIT", 6: "BOS", 7: "BUF", 8: "MTL",
    9: "OTT", 10: "TOR", 12: "CAR", 13: "FLA", 14: "TBL", 15: "WSH", 16: "CHI", 17: "DET",
    18: "NSH", 19: "STL", 20: "CGY", 21: "COL", 22: "EDM", 23: "VAN", 24: "ANA", 25: "DAL",
    26: "LAK", 28: "SJS", 29: "CBJ", 30: "MIN", 52: "WPG", 54: "VGK", 55: "SEA", 59: "UTA",
}
