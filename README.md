# nhl-contracts

[![PyPI](https://img.shields.io/pypi/v/nhl-contracts?label=PyPI)](https://pypi.org/project/nhl-contracts/)
[![npm](https://img.shields.io/npm/v/nhl-contracts?label=npm)](https://www.npmjs.com/package/nhl-contracts)
[![dataset](https://img.shields.io/github/v/release/ehlersio/nhl-contracts?filter=contracts&label=dataset&display_name=tag)](https://github.com/ehlersio/nhl-contracts/releases/tag/contracts)
[![update-contracts](https://github.com/ehlersio/nhl-contracts/actions/workflows/update-contracts.yml/badge.svg)](https://github.com/ehlersio/nhl-contracts/actions/workflows/update-contracts.yml)
[![ci](https://github.com/ehlersio/nhl-contracts/actions/workflows/ci.yml/badge.svg)](https://github.com/ehlersio/nhl-contracts/actions/workflows/ci.yml)
[![data: CC0](https://img.shields.io/badge/data-CC0_1.0-lightgrey)](LICENSE-DATA)
[![code: MIT](https://img.shields.io/badge/code-MIT-blue)](LICENSE)

An open, provenance-first dataset of NHL player contracts: term, cap hit, total
value, year-by-year salary and signing-bonus structure, and trade clauses, with
every number tied to the public document it came from and a confidence tier that
says what kind of document that was. Conflicting reports are kept side by side,
never silently resolved.

**Data: CC0 1.0** (`LICENSE-DATA`). **Code: MIT** (`LICENSE`).

Status: first release 2026-10-23. The dataset is on the rolling
[`contracts` release](https://github.com/ehlersio/nhl-contracts/releases/tag/contracts);
the loaders below fetch it.

## Using the data

Python (`pip install nhl-contracts`; add `[parquet]` for pyarrow):

```python
import nhl_contracts

meta = nhl_contracts.timestamp()                  # updated_at, row counts, tiers
contracts = nhl_contracts.load("contracts")       # list of dicts
seasons = nhl_contracts.load("contract_seasons", fmt="parquet")   # pyarrow.Table
```

JavaScript (`npm install nhl-contracts`, Node 18+):

```js
import { load, timestamp } from "nhl-contracts";
const contracts = await load("contracts");
const clauses = await load("clauses");
```

Or download the files directly from the release; every table comes as
parquet, CSV and JSON. Neither package ships data, so they rarely need a
version bump; `timestamp.json` tells you how fresh the files are.

## Sources and tiers

| Tier | Source | What it gives | How it is read |
|---|---|---|---|
| A | Club press releases on NHL.com | term, type, often AAV or cap hit, sometimes a full breakdown (New Jersey, Carolina) | NHL.com Forge content API |
| B | NHL.com staff stories | term, total value, AAV, NHL player id | Forge content API |
| C | ESPN transaction wire | term, type, two-way, PTO; almost never money | ESPN site API |
| D | PuckPedia's public Bluesky posts | cap hit, year-by-year salary and signing bonus, clauses with list sizes, agent, expiry status | Bluesky public API (no account) |
| D | r/hockey post titles quoting insiders and club accounts | term, AAV, cap hit or total as quoted, type, one/two-way; the bracket tag (`[Friedman]`, `[NY Rangers PR]`) is the author | Reddit's public Atom feeds (new posts and a signings search), no account; titles quoting PuckPedia, CapWages or Spotrac are skipped |
| E | CapFriendly pages as preserved by the Internet Archive (site closed July 2024) | full contract history to July 2024: per-season cap hit, base salary, signing and performance bonuses, minors salary, clauses | Wayback Machine, one paced request at a time |

Tiers A–C are official or first-hand publications. D and E are agent-fed
databases that the public reads second-hand; they are the only public carriers
of clauses and bonus schedules, so the dataset labels them rather than omitting
them. Nothing is read from puckpedia.com, capwages.com, spotrac.com or
thestanleycap.com, and nothing from a paid API.

## What you get, and what you will not

- Every signing from the 2026 offseason on, with term and type, from the
  official tiers; money for about two thirds of them (several clubs state no
  figures), topped up by tier D for nearly all of the rest.
- Year-by-year structure (salary, signing bonus, bonuses, minors salary) for
  roughly half of recent signings (tier D posts that include it, plus the few
  clubs that publish it) and for every contract that was active in July 2024
  (tier E).
- Clauses (NMC, NTC, modified NTC with list size) from tier D posts and tier E
  team pages. Modified-clause list sizes are sometimes never made public.
- Dead cap (`dead_cap`): salary retained on traded players and buyout charges,
  per team and season, from PuckPedia's trade and buyout posts (tier D, from
  mid-2023) and the archived CapFriendly team pages (tier E, every charge
  running in July 2024 with its full remaining schedule). A retention whose
  post does not say how many seasons are left is carried through the end of
  the player's contract in this dataset and flagged. Not covered: LTIR,
  performance-bonus overages, recapture penalties, and retentions that only
  the trade call itself announced.
- Not covered: contracts signed after July 2024 that no tier A–D source
  described; performance-bonus achievement; anything the sources got wrong
  (figures are kept as written and flagged when they fail a sanity check).

## Files (written to `dist/`, later attached to a GitHub Release tag)

| File | Grain | Notes |
|---|---|---|
| `contracts.parquet` / `.csv` / `.json` | one row per contract | merged across sources; a field that sources disagree on is null and named in `conflicts` |
| `claims.*` | one row per (source document, contract) | the raw extraction, with evidence sentence and flags; this is where the conflicting values live |
| `contract_seasons.*` | one row per (contract, season) | cap hit, AAV, base salary, signing bonus, performance bonus, total salary, minors salary, one-way, clause label; merged with `conflicts` |
| `season_claims.*` | one row per (source, contract, season) | the per-source season rows |
| `clauses.*` | one row per (contract, clause kind, season range) | NMC / NTC / M-NTC with list size and list type when stated |
| `clause_claims.*` | one row per (source, contract, clause) | the per-source clause rows |
| `dead_cap.*` | one row per (team, kind, player, season) | cap charges that are not roster contracts: `retained` salary on a traded player and `buyout` charges, with the retained `pct` and the acquiring `counterparty` when stated; merged with `conflicts` |
| `dead_cap_claims.*` | one row per (source, team, player, season) | the per-source dead-cap rows with evidence and flags |
| `sources.csv` | one row per source document | URL, publisher, author, published time, tier |
| `gaps.csv` / `.json` | one row per current NHL roster player the dataset does not fully cover | `gap` = `no_contract` / `no_money` / `no_structure`; `status` says whether the player has older rows, is mentioned in the raw store (a parsing or matching gap) or is absent from every source; hints quote the latest ESPN row, NHL.com headline and Bluesky post that name them |
| `timestamp.json` | run metadata | `updated_at`, counts per table and tier, file list, licence |

## Data dictionary

### `contracts`

| column | type | meaning |
|---|---|---|
| `contract_key` | string | stable grouping key: NHL player id (or normalized name) + team + signing month |
| `player_name` | string | as written by the best-tier source |
| `player_id` | int, nullable | NHL player id, when a source tags or links the player (about a third of club releases, nearly all NHL.com stories) |
| `team` | string | NHL tri-code (`BOS`, `UTA`, …), from the Forge `teamid-` tag or the club author tag |
| `signed_at` | date | the contract's signing date when a source states one (`signing_date_stated` true, tier E), else the publication date of the earliest source |
| `signing_date_stated` | bool | see above |
| `contract_type` | string | `ELC`, `standard`, `extension`, `PTO` |
| `term_years` | int, nullable | contract length |
| `aav_usd` | int, nullable | stated average annual value |
| `cap_hit_usd` | int, nullable | stated "cap hit" (kept apart from AAV: deferred-money deals under the pre-2026 CBA can differ) |
| `total_usd` | int, nullable | stated total value; for one-year deals equals the stated salary |
| `nhl_salary_usd` / `minors_salary_usd` | int, nullable | the two halves of a two-way contract when stated |
| `one_way` | bool, nullable | true = one-way, false = two-way, null = not stated |
| `start_season` / `end_season` | string, nullable | `2026-27` style, stated or derived (`through the 2029-30 season` + term) |
| `expiry_status` | string, nullable | UFA / RFA (and variants) at expiry, when a source states it |
| `agent` | string, nullable | the player's representative when a source names one (tier D) |
| `re_signed` | bool | the source used "re-sign" wording |
| `conflicts` | string | `;`-separated field names on which sources disagree (those fields are null) |
| `n_sources` | int | number of source documents merged |
| `best_tier` | string | best confidence tier among the sources |
| `tiers` | string | every tier that contributed, e.g. `ADE` |
| `source_ids`, `claim_ids` | string | `;`-separated links into `sources.csv` and `claims.*` |
| `flags` | string | union of claim flags (see below) |

### `claims`

Same contract columns, plus `claim_id`, `source_id`, `source_url`, `publisher`,
`published_at`, `confidence_tier`, `extracted_by` (`regex`, or `llm` if a
fallback extractor is registered), `evidence` (the sentence the values came
from) and `flags`.

### `contract_seasons`

| column | meaning |
|---|---|
| `season` | `2026-27` style |
| `cap_hit_usd`, `aav_usd` | as stated for that season |
| `base_salary_usd` | NHL salary excluding bonuses |
| `signing_bonus_usd`, `performance_bonus_usd` | the performance figure is the maximum attainable, not what was earned |
| `total_salary_usd` | base plus signing bonus when both are known or the source states the total |
| `minors_salary_usd` | the AHL side of a two-way season |
| `one_way` | per season; a contract can switch |
| `clause` | the raw clause label for that season, e.g. `NMC`, `15 team M-NTC` |

### `clauses`

| column | meaning |
|---|---|
| `kind` | `NMC` (no-move), `NTC` (full no-trade), `M-NTC` (modified: a list of teams) |
| `season_from`, `season_to` | the seasons the clause covers |
| `list_size` | number of teams on a modified list, when stated |
| `list_type` | `no-trade` when the list names teams the player cannot be traded to; null when the source does not say which kind of list it is |

### `dead_cap`

| column | meaning |
|---|---|
| `team` | the team carrying the charge (the retaining team, or the team that bought the player out) |
| `kind` | `retained` or `buyout` |
| `player_name`, `player_id` | the player; the id when a roster list or a source gives it |
| `counterparty` | for a retention, the team that acquired the player, when the source names it (PuckPedia trade posts; the archived team pages do not) |
| `season` | the season the charge applies to |
| `cap_hit_usd` | the charge for that season, as stated, or the stated percentage of the player's cap hit on the trade line (`derived:charge_from_pct`) |
| `pct` | the retained share when stated, or derived from a stated amount and cap hit (`derived:pct_from_amounts`); null for buyouts |
| `contract_key` | the player's contract in `contracts` when the span was filled from it |
| `event_date` | the trade or buyout date, when a dated post is the source |
| flags | `end_season_unknown` (the post did not say how many seasons remain) with `derived:span_from_contract` once filled; `retainer_unknown` (a three-team deal the parser could not attribute); `charge_unknown` (a percentage with no cap hit on the line); `team_page_dead_cap` (tier E rows) |

### Flags

Flags are `;`-separated on claims and unioned on merged rows. The important ones:

| flag | meaning |
|---|---|
| `derived:total_one_year`, `derived:aav_one_year`, `derived:aav_from_nhl_salary` | arithmetic identity on a one-year deal, not a separate statement by the source |
| `total_vs_aav_mismatch` | term × AAV differs from the stated total by more than 2% (one real case so far is a club typo, "$58,500,00") |
| `below_league_minimum` | a stated salary is below the CBA minimum for that season (also catches club typos such as "$850,00") |
| `per_season_breakdown` | the source gives a year-by-year or bonus schedule; the AAV may not be stated and is left null |
| `multi_player_story` | several players signed in one release; fields were read per player |
| `attributed_report` | the sentence cites an agent or "sources" rather than the club |
| `arbitration` | the story mentions arbitration |
| `season_span_mismatch` | stated start, end and term do not agree |
| `multi_year_unspecified` | "multi-year" with no number |
| `guaranteed:<usd>` | a guarantee stated for a two-way deal; never stored as a salary |
| `signing_bonus_total:<usd>` | a contract-long bonus total the source did not split by season |
| `season_sum_mismatch`, `total_vs_cap_hit_mismatch`, `total_vs_seasons_mismatch` | the stated total does not equal the sum of the parts (several are club typos) |
| `start_season_assumed` | tier D: the post did not say when the deal starts; a non-extension signed May–June or later is placed in the coming season, an extension in the season after that |
| `surname_only`, `name_from_link`, `first_name_from_link` | tier D: the post named only a surname; the full name came from the linked player page when possible |
| `signing_date_unknown`, `term_is_remaining_seasons`, `not_on_player_page` | tier E: a row from a team page only, dated by the snapshot; its term is the number of visible seasons |
| `total_includes_performance_bonuses` | tier E: CapFriendly's contract value includes maximum performance bonuses (entry-level deals) |
| `retained_salary_adjusted` | tier E: the cap hit shown is after retention in a trade |
| `unconfirmed` | tier E: the page said the contract details were not yet confirmed |
| `35_plus`, `franchise_relocated`, `varying_cap_hit`, `two_way_minors_salary_shown` | tier E oddities kept as written |
| `player_id_from_roster`, `player_id_by_alias` | the NHL id was attached by name (and team) from the current roster and prospect lists, not stated by the source; `by_alias` means the first name was folded ("Zach" for "Zachary") or a middle name dropped |
| `derived:span_from_signing_date` | no source stated the seasons; `start_season`/`end_season` were placed from the signing date and term (a deal signed July–June belongs to the season starting that July; an extension to the season after) |
| `ref:<url>` | tier D: the puckpedia.com link in a PuckPedia post, or the outbound link (usually the quoted X post) of an r/hockey post; dropped from merged rows |
| `insider:<tag>`, `report` | tier D (r/hockey): the quoted account, and a title phrased as a report ("Report:", a reporter's "agreed"/"signing") rather than an announcement |
| `money_unlabeled`, `multiple_amounts`, `term_mismatch` | tier D (r/hockey): amounts the title does not tie to a role, several amounts, or a term stated twice differently |

### How rows are merged

Claims are the same contract when they name the same player (NHL id when both
have one, else the accent-folded first and last name with common short forms
folded, so "Zach Werenski" meets "Zachary Werenski"; a surname-only post joins
a same-team claim within 45 days) on the same team and were published within 45 days of each
other, or cover the same seasons when one of them is an archived row with no
signing date. Values are taken from every tier; a disagreement between any two
sources is reported in `conflicts`, not resolved. `best_tier` is the best tier
present, not a vote.

## Coverage

### Official tiers, measured on the committed fixtures (2026-10-23)

The fixtures under `tests/fixtures/forge/` are 253 real Forge responses: every
2026 story whose headline looks like a signing across all 32 clubs, including
15 non-player stories (coach extensions, radio rights, affiliations, an AHL
contract, an emergency backup goalie) that the parser must reject.

| measure | result |
|---|---|
| 2026 player-signing stories | 220 |
| … that yield at least one contract row | 220 (100%) |
| … with the term read | 218 (99.1%) |
| stories that state any dollar figure | 150 of 220 (68%) |
| … with AAV / cap hit / total / salary read | 144 (96.0%) |
| … with AAV or cap hit read | 141 |
| … with total value read | 78 |
| claims with an NHL player id | 80 of 242 |
| non-player stories wrongly accepted | 0 |

The six money misses are structures the regex cannot total (per-season salary
plus signing-bonus schedules, mixed one-way/two-way years, previous-contract
mentions in a "terms not disclosed" story). Those stories are flagged and
routed to the LLM fallback hook, which is unwired by default.

Clubs that state no money in 2026 releases: Rangers, Islanders, Panthers,
Lightning, Red Wings, Predators, Avalanche, most Senators and Stars releases.
For those, AAV arrives from NHL.com's staff (tier B) for notable deals and from
tier D for the rest.

Official releases with a per-season breakdown: 31 of the 220 stories (50 season
rows), almost all New Jersey, Carolina and two-way one-year deals.

### Tier D, measured on 694 PuckPedia posts (2025-10 to 2026-10)

| measure | result |
|---|---|
| posts classified as signings | 459 |
| … with team, term and a cap hit / AAV / salary | 457 (99.6%) |
| posts with a year-by-year breakdown | 223, of which 222 yield exactly `term` season rows |
| season rows / clause rows | 872 / 93 |

### Tier E

Measured on the live store after the crawl; see `timestamp.json` and the
section "Live store" below.

### Live store (2026-10-23, after the full backfill)

| measure | result |
|---|---|
| merged contracts | 6,961 (claims 9,679 from 6,331 source documents) |
| … with a stated signing date | 3,855 (tier E player pages) |
| … with an NHL player id | 3,474 (ids come from current rosters and prospect lists, so retired players lack them) |
| … with a cap hit or AAV | 5,096 |
| … with per-season rows | 4,876 (12,925 season rows) |
| … with clauses | 594 (941 clause rows) |
| contracts covering 2026-27 or later | 1,165; 1,015 with money, 962 with season rows, 876 with a player id |
| signings since June 2026 | 485; 316 with money, 322 with season rows, 44 with clauses |
| contracts where two sources disagree on a field | 60 |
| claims by tier | A 542 · B 556 · C 3,583 · D 840 · E 4,158 |
| CapFriendly pages read | 33 team pages, 1,494 player pages (2 missing, 5 unreadable) |

Tier E carries the contract history back to the early 2000s for every player
who was on a roster, prospect list or buyout table in July 2024. Tier C (ESPN)
covers every depth signing since 2016 but mostly without money. Tier D fills
money and structure for recent signings that clubs did not disclose.

Common tier E flags: `total_includes_performance_bonuses` (CapFriendly's
value column adds maximum bonuses on entry-level deals, 578 rows),
`total_vs_seasons_mismatch` (the same effect, 764 rows), `varying_aav`
(two-way or bonus-laden deals, 521 rows).

## How it works

```
nhl_contracts/
  collectors/nhl_forge.py           poll Forge lists per team (and per player for backfill), store raw stories
  collectors/espn_transactions.py   ESPN transactions by date range
  collectors/bluesky_puckpedia.py   PuckPedia's public Bluesky feed (incremental or back to 2023-07)
  collectors/reddit_hockey.py       r/hockey Atom feeds: post titles quoting insiders (last week only)
  collectors/wayback_capfriendly.py Internet Archive snapshots of CapFriendly team and player pages
  normalize/model.py                Claim, SeasonClaim, ClauseClaim, Source, tiers
  normalize/contracts.py            NHL.com + ESPN regex extraction, incl. per-season breakdowns
  normalize/puckpedia.py            post text -> claims, seasons, clauses
  normalize/reddit.py               r/hockey titles -> claims (rumours and non-NHL leagues rejected)
  normalize/capfriendly.py          archived HTML -> contract history, seasons, clauses
  normalize/merge.py                cross-source merge; conflicts named, never resolved
  normalize/llm_fallback.py         OpenRouter extractor for stories the regex cannot read (off without a key)
  publish/export.py                 parquet / csv / json / timestamp.json
  store.py                          SQLite raw store (idempotent upserts)
  teams.py                          team ids, roster and prospect player ids
  run.py                            python -m nhl_contracts.run [collect|backfill|normalize|export|all]
```

### Sources and their quirks

- **NHL.com Forge** `https://forge-dapi.d3.nhle.com/v2/content/en-us/stories?tags.slug=<tags>`.
  Every list returns at most 25 items and ignores paging and date filters, so
  the collector polls three lists per team (`transactions,teamid-N`,
  `press-release,teamid-N`, `teamid-N`) plus the league-wide `transactions`
  list, about 100 requests a cycle. Tagging is inconsistent per club: Boston's
  releases carry `press-release` but not `transactions`; NHL.com staff signing
  stories often carry neither, only `teamid-N` and `playerid-N`. Story bodies
  come from `.../stories/<slug>` as markdown parts with `<forge-entity>` player
  markup, which supplies player ids.
- **ESPN** `https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/transactions?dates=YYYYMMDD-YYYYMMDD&limit=1000`.
  Term and type for nearly every signing, dollars for almost none. Used as a
  cross-check and as a net for clubs whose NHL.com tagging misses a release.
- Team ids come from `https://api.nhle.com/stats/rest/en/team` filtered to the
  abbreviations in `https://api-web.nhle.com/v1/standings/now`. Utah appears
  under two ids (59 and 68); both map to `UTA`. Player ids for name matching
  come from `/v1/roster/{team}/current` and `/v1/prospects/{team}`.
- **PuckPedia on Bluesky** `https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=puckpedia.com`.
  Posts are formulaic ("signed 24 y/o F … to a three year $3.95M cap hit
  contract extension / Yr 1: $2.2M Salary, $1.5M Signing Bonus / … / 15 Team NTC
  in Yr 5 & 6 / Rep'd by …"). Reposts and other authors are skipped. Team
  hashtags are mapped to tri-codes (typos included); an unknown tag leaves
  `team` null with `team_unmapped`.
- **r/hockey on Reddit** `https://www.reddit.com/r/hockey/new.rss?limit=100`, read
  hourly (that covers the subreddit's volume). Post titles quote insider posts
  from X verbatim, which is the only free, terms-compliant way to read them; the
  Reddit permalink is the cited source and the bracket tag the author. Rumours
  ("closing in on", "expected to"), other leagues, coaches and executives yield
  nothing. Reddit answers its search feed with 429 from cloud runners, so the
  signings search is only used for a backfill from a home connection.
- **CapFriendly via the Wayback Machine**. One CDX query per page finds the
  2024 captures; the latest valid one before the July 2024 shutdown is fetched
  with the `id_` flag (original bytes). Team pages give every player's
  per-season cap hit, salary, bonuses and clause tooltip (the tooltips hold the
  list sizes); player pages give signing dates, signing GM, value and the
  contract history. Requests are spaced 2.5 s apart with long back-off; a full
  crawl of about 1,600 player pages takes a few hours and is resumable. Many
  player pages were last captured before July 1, 2024, so a July 2024 signing
  can be missing from the player page and present only on the team page.

### Running

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m nhl_contracts.run all
```

This polls NHL.com (about 100 Forge list requests plus one body fetch per new
story), ESPN and the Bluesky feed, normalizes everything in `data/raw.sqlite`
and writes `dist/`. `normalize` works offline from the store. Re-runs only fetch
what is new or updated.

One-off history (resumable; the Wayback crawl is slow by design):

```bash
.venv/bin/python -m nhl_contracts.run backfill --sources bluesky,espn,forge
```

```bash
.venv/bin/python -m nhl_contracts.run backfill --sources wayback
```

```bash
.venv/bin/pytest
```

### LLM fallback

Stories where the regex finds a signing but cannot attach a stated dollar
figure (bonus schedules, mixed one-way/two-way years, prose) are sent to a model
through OpenRouter when `OPENROUTER_API_KEY` is set; `NHL_CONTRACTS_LLM_MODEL`
picks the model (default `anthropic/claude-haiku-5.5`, about $0.10 per million
input tokens). The model is only allowed to read: every amount, term and season
it returns is checked against the story text and dropped if the text does not
state it, so a computed AAV or a guessed season never enters the dataset. Rows
it touched carry `extracted_by` = `llm` or `regex+llm` and the flag
`llm:<model>`. About 277 stories in the current store are eligible, roughly 40 a
season going forward. `--no-llm` disables it for a run.

### Gaps report

Coverage is measured against the 800 players on current NHL rosters (the
roster and prospect lists from the NHL API are stored with the players).
`gaps.csv` on every release lists the roster players whose current contract
is missing, has no money, or has no per-season rows, with what the raw store
knows about each, so the remaining work is a list rather than a percentage.
`timestamp.json` carries the counts (`n_gaps_*`).

### Repository hygiene

Dependabot (pip, npm in `js/`, GitHub Actions, weekly, grouped), CodeQL default
setup (Python, JavaScript, Actions), secret scanning with push protection,
Dependabot security updates, workflow actions pinned to commit SHAs, `pip-audit`
and `npm audit` in CI, dependency review on pull requests, and a ruleset that
blocks deleting or force-pushing `main`. Direct pushes to `main` are allowed
because the hourly workflow commits the refreshed raw rows.

### Where the raw data lives

`data/raw.sqlite` is a working cache. Its portable copies are:

- `data/raw/*.jsonl`, committed to this repository: every NHL.com story, ESPN
  row, Bluesky post, player and team ever collected, one sorted file per table
  so each run is a readable diff. These are the only copies of stories that have
  aged out of Forge's 25-item lists. About 30 MB today, growing roughly 1 MB a
  month.
- `data/wayback_pages.sqlite` (25 MB), the one-off CapFriendly archive crawl,
  kept as an asset on the `contracts` release rather than in git.

`run load` rebuilds the cache from both; `collect`, `backfill` and `all` load
an empty cache automatically and refresh the JSONL afterwards (`--no-dump` to
skip). `run dump --sources wayback` also rewrites the archive file after a new
crawl.

### Publishing

`.github/workflows/update-contracts.yml` mirrors nflverse-data: rebuild the
cache from the committed JSONL plus the archive file on the `contracts`
release, run the collector, commit the refreshed JSONL back to the repository,
and upload `dist/*` to the same tag with `--clobber`, so clients always
download the latest files from one fixed URL. The real schedule is `workflow_dispatch`
called by the EyeWall Cloudflare Worker (hourly in season, every 15 minutes
from July 1 to 15, since GitHub cron runs hours late); a daily GitHub cron is
the safety net. The workflow passes `OPENROUTER_API_KEY` from the repository
secrets when it exists.

Repository: `ehlersio/nhl-contracts`; packages: `nhl-contracts` on PyPI and npm.

The loaders live in `nhl_contracts/load.py` and `js/`. `.github/workflows/publish.yml`
publishes both when a `v*` tag is pushed, through each registry's trusted
publishing (OpenID Connect, no stored tokens), which has to be enabled once on
pypi.org and npmjs.com as the workflow's comments describe.

## Caveats

- `signed_at` is the publication date of the source unless a source states the
  signing date (tier E does). Clubs usually publish the same day.
- Tier D and E figures are what an agent or team told a database, re-published
  here with a link to the public page it was read from. They are not official
  and they are occasionally wrong; the flags say when they fail a sanity check.
- AAV versus cap hit: both are kept as written. Under the CBA in force until
  2026-09-16 deferred money could make them differ; new contracts cannot defer.
- Money is never inferred. Where a source states no figure, the field is null.
  Where the regex cannot read a stated figure, the story is flagged so an
  extractor can be pointed at it.
- The Forge API and ESPN feed are undocumented and may change without notice.
