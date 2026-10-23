# nhl-contracts

An open, provenance-first dataset of NHL player contracts built only from
official public sources: club press releases and NHL.com staff stories (via the
NHL.com Forge content API) and ESPN's transaction wire. Every number carries the
URL it came from and a confidence tier. Conflicting reports are kept side by
side, never silently resolved.

**Data: CC0 1.0** (`LICENSE-DATA`). **Code: MIT** (`LICENSE`).

Status: Phase 1, pre-release. Nothing is published yet; the GitHub Actions
workflow that would publish to a Releases tag is checked in but not scheduled.

## What is in scope (Phase 1)

| Field | Source | Typical availability |
|---|---|---|
| player, team, signing date | club release, NHL.com story, ESPN wire | always |
| contract type (ELC / standard / extension / PTO), re-signing | same | always |
| term (years) | same | ~99% of signing stories |
| AAV or cap hit | club release or NHL.com story | ~65% of signings (many clubs do not disclose money) |
| total value | mostly NHL.com staff stories | ~35% of signings |
| one-way / two-way, NHL and minors salary | club release | when stated |
| start and end season | stated or derived from term + "through the 2029-30 season" | when stated |

**Out of scope, deliberately:** no-trade / no-movement clauses, year-by-year
salary and signing-bonus schedules, performance bonuses, retained salary,
buyouts. No official source publishes them; they reach the public only through
agent-fed sites. This dataset never scrapes PuckPedia, CapWages, Spotrac or
TheStanleyCap. A later phase may mirror publicly posted structure data with
attribution, as a separate, clearly labelled tier.

## Files (written to `dist/`, later attached to a GitHub Release tag)

| File | Grain | Notes |
|---|---|---|
| `contracts.parquet` / `.csv` / `.json` | one row per contract | merged across sources; a field that sources disagree on is null and named in `conflicts` |
| `claims.parquet` / `.csv` / `.json` | one row per (source document, contract) | the raw extraction, with evidence sentence and flags; this is where the conflicting values live |
| `sources.csv` | one row per source document | URL, publisher, author, published time, tier |
| `timestamp.json` | run metadata | `updated_at`, counts, file list, licence |

## Data dictionary

### `contracts`

| column | type | meaning |
|---|---|---|
| `contract_key` | string | stable grouping key: NHL player id (or normalized name) + team + signing month |
| `player_name` | string | as written by the best-tier source |
| `player_id` | int, nullable | NHL player id, when a source tags or links the player (about a third of club releases, nearly all NHL.com stories) |
| `team` | string | NHL tri-code (`BOS`, `UTA`, …), from the Forge `teamid-` tag or the club author tag |
| `signed_at` | date | publication date of the earliest source (UTC) |
| `contract_type` | string | `ELC`, `standard`, `extension`, `PTO` |
| `term_years` | int, nullable | contract length |
| `aav_usd` | int, nullable | stated average annual value |
| `cap_hit_usd` | int, nullable | stated "cap hit" (kept apart from AAV: deferred-money deals under the pre-2026 CBA can differ) |
| `total_usd` | int, nullable | stated total value; for one-year deals equals the stated salary |
| `nhl_salary_usd` / `minors_salary_usd` | int, nullable | the two halves of a two-way contract when stated |
| `one_way` | bool, nullable | true = one-way, false = two-way, null = not stated |
| `start_season` / `end_season` | string, nullable | `2026-27` style, stated or derived (`through the 2029-30 season` + term) |
| `re_signed` | bool | the source used "re-sign" wording |
| `conflicts` | string | `;`-separated field names on which sources disagree (those fields are null) |
| `n_sources` | int | number of source documents merged |
| `best_tier` | string | best confidence tier among the sources |
| `source_ids`, `claim_ids` | string | `;`-separated links into `sources.csv` and `claims.*` |
| `flags` | string | union of claim flags (see below) |

### `claims`

Same contract columns, plus `claim_id`, `source_id`, `source_url`, `publisher`,
`published_at`, `confidence_tier`, `extracted_by` (`regex`, or `llm` if a
fallback extractor is registered), `evidence` (the sentence the values came
from) and `flags`.

### Flags

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

### Provenance tiers

| tier | source | how it is recognised |
|---|---|---|
| A | club press release published on NHL.com | Forge author tag `xxx-created` where `xxx` is a club code (`bos-created`) |
| B | NHL.com staff story | author tag `nhl-created` |
| C | anything else, including ESPN's transaction wire | |

Within a merged contract, values are taken from all tiers; a disagreement
between any two sources is reported, not resolved.

## Coverage, measured on the committed fixtures (2026-10-23)

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
For those, AAV arrives only when NHL.com's staff writes a story (tier B), which
happens for most notable deals and rarely for two-way depth signings.

## How it works

```
nhl_contracts/
  collectors/nhl_forge.py        poll Forge lists per team, fetch story bodies, store raw
  collectors/espn_transactions.py fetch ESPN transactions by date range, store raw rows
  normalize/contracts.py         regex extraction -> claims; merge claims -> contracts
  normalize/llm_fallback.py      hook for an LLM extractor (nothing registered, no key)
  publish/export.py              parquet / csv / json / timestamp.json
  store.py                       SQLite raw store (idempotent upserts)
  teams.py                       the 32 team ids from the NHL stats team list + standings
  run.py                         python -m nhl_contracts.run [collect|normalize|export|all]
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
  under two ids (59 and 68); both map to `UTA`.

### Running

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m nhl_contracts.run all
```

This polls both sources (about 100 Forge list requests, plus one body fetch per
new story), normalizes everything in `data/raw.sqlite` and writes `dist/`.
`normalize` and `export` work offline from the store. Re-runs only fetch what is
new or updated.

```bash
.venv/bin/pytest
```

### Publishing (not activated)

`.github/workflows/update-contracts.yml` mirrors nflverse-data: restore the raw
store from the `contracts` release, run the collector, upload `dist/*` and the
store back to the same tag with `--clobber`, so clients always download the
latest files from one fixed URL. The `schedule` block is commented out. When it
is switched on, prefer an external dispatcher calling `workflow_dispatch`
(GitHub cron can run hours late, and a July 1 hour can exceed a 25-item list).

Thin npm and PyPI loaders that download the release files are planned but not
written; neither package exists yet.

## Caveats

- `signed_at` is the publication date of the source, not the date on the
  contract. Clubs usually publish the same day.
- AAV versus cap hit: both are kept as written. Under the CBA in force until
  2026-09-16 deferred money could make them differ; new contracts cannot defer.
- Money is never inferred. Where a source states no figure, the field is null.
  Where the regex cannot read a stated figure, the story is flagged so an
  extractor can be pointed at it.
- The Forge API and ESPN feed are undocumented and may change without notice.
