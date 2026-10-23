"""Coverage gate over every 2026 signing story in the fixtures (real Forge responses)."""

import re

from nhl_contracts.normalize.contracts import extract_story
from nhl_contracts.normalize.text import clean

SIGNING_HEADLINE = re.compile(r"\b(sign|signs|signed|re-sign|re-signs|agree[sd]? to terms|inks)\b", re.I)
# Stories whose headline looks like a signing but is not a player contract (or has
# no contract sentence at all): coaches, broadcast deals, affiliates, features.
NOT_PLAYER_CONTRACTS = (
    "minnesota-wild-signs-head-coach", "ottawa-senators-sign-travis-green", "travis-green-signs-deal-as-ottawa-coach",
    "senators-players-react", "release-blackhawks-and-wgn-radio", "tampa-bay-lightning-syracuse-crunch",
    "jets-sign-cole-kehler", "release-condors-sign-eemil-vinni", "dallas-stars-hope-to-begin",
    "philadelphia-flyers-and-nexgen",
    "golden-knights-tahoe-knight-monsters", "debrincat-shows-faith",
    "multi-year-contract-extension-for-martin-st-louis",
    "vancouver-canucks-sign-goaltender-chris-driedger-to-a-pto-hire",
)


def test_signing_story_coverage(stories, teams):
    positives = [s for s in stories if s.content_date >= "2026-01-01" and SIGNING_HEADLINE.search(s.headline)
                 and not s.slug.startswith(NOT_PLAYER_CONTRACTS)]
    assert len(positives) >= 200
    with_claims = []
    for s in positives:
        claims, _ = extract_story(s, teams)
        assert claims, f"no claim for {s.slug}"
        with_claims.append((s, claims))
    term_ok = sum(1 for _, cs in with_claims if all(c.term_years or c.contract_type == "PTO" for c in cs))
    dollar = [(s, cs) for s, cs in with_claims if "$" in clean(s.body)]
    money_ok = sum(1 for _, cs in dollar
                   if all(c.aav_usd or c.cap_hit_usd or c.total_usd or c.nhl_salary_usd for c in cs))
    assert term_ok / len(with_claims) >= 0.90, f"term {term_ok}/{len(with_claims)}"
    assert money_ok / len(dollar) >= 0.90, f"money {money_ok}/{len(dollar)}"


def test_non_player_stories_are_rejected(stories, teams):
    for s in stories:
        if s.slug.startswith(NOT_PLAYER_CONTRACTS):
            claims, _ = extract_story(s, teams)
            assert claims == [], s.slug


def test_every_claim_has_a_sane_name_and_team(stories, teams):
    for s in stories:
        for c in extract_story(s, teams)[0]:
            assert 2 <= len(c.player_name.split()) <= 4, (s.slug, c.player_name)
            assert c.player_name[0].isupper() and not c.player_name.endswith("."), (s.slug, c.player_name)
            assert c.team is not None, s.slug
            assert c.confidence_tier in "ABC"
