"""The LLM fallback may only read numbers the story states; everything else is dropped."""

import json

import httpx
from conftest import load_story

from nhl_contracts.normalize import llm_fallback
from nhl_contracts.normalize.contracts import extract_story, normalize_all
from nhl_contracts.normalize.llm_fallback import OpenRouterExtractor, parse_response, verify_row

KOCHETKOV_TEXT = ("The team has signed goaltender Pyotr Kochetkov to a two-year contract extension. The deal will pay "
                  "Kochetkov $2.4 million with a $1.5 million signing bonus for the 2027-28 season and $3 million with "
                  "a $1 million signing bonus for the 2028-29 season.")


def test_verify_row_keeps_stated_numbers_only():
    row = {"player_name": "Pyotr Kochetkov", "team_abbrev": "CAR", "contract_type": "extension", "term_years": 2,
           "aav_usd": 3_950_000, "total_usd": 7_900_000, "start_season": "2027-28", "end_season": "2028-29",
           "seasons": [{"season": "2027-28", "base_salary_usd": 2_400_000, "signing_bonus_usd": 1_500_000},
                       {"season": "2028-29", "base_salary_usd": 3_000_000, "signing_bonus_usd": 1_000_000},
                       {"season": "2029-30", "base_salary_usd": 3_000_000}]}
    out = verify_row(row, KOCHETKOV_TEXT)
    assert out["term_years"] == 2 and out["aav_usd"] is None and out["total_usd"] is None  # computed, not stated
    assert (out["start_season"], out["end_season"]) == ("2027-28", "2028-29")
    assert [s["season"] for s in out["seasons"]] == ["2027-28", "2028-29"]  # 2029-30 is not in the text
    assert out["seasons"][0] == {"season": "2027-28", "base_salary_usd": 2_400_000, "signing_bonus_usd": 1_500_000,
                                 "performance_bonus_usd": None, "minors_salary_usd": None}


def test_verify_row_rejects_unnamed_player_and_empty_rows():
    assert verify_row({"player_name": "Someone Else", "term_years": 2}, KOCHETKOV_TEXT) is None
    assert verify_row({"player_name": "Pyotr Kochetkov", "aav_usd": 123}, KOCHETKOV_TEXT) is None


def test_parse_response_tolerates_fences():
    assert parse_response('```json\n[{"player_name": "A B"}]\n```') == [{"player_name": "A B"}]
    assert parse_response("no json here") == []


def _fake_openrouter(answer):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "anthropic/claude-haiku-5.5" and "Kochetkov" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 120}})
    return httpx.MockTransport(handler)


def test_openrouter_extractor_fills_regex_gaps(teams):
    story = load_story("canes-sign-kochetkov")
    answer = [{"player_name": "Pyotr Kochetkov", "team_abbrev": "CAR", "contract_type": "extension", "term_years": 2,
               "aav_usd": 3_950_000, "start_season": "2027-28", "end_season": "2028-29",
               "seasons": [{"season": "2027-28", "base_salary_usd": 2_400_000, "signing_bonus_usd": 1_500_000},
                           {"season": "2028-29", "base_salary_usd": 3_000_000, "signing_bonus_usd": 1_000_000}]}]
    extractor = OpenRouterExtractor("test-key", transport=_fake_openrouter(answer))
    llm_fallback.register(extractor)
    try:
        claims, _ = extract_story(story, teams)
        assert extractor.calls == 1 and extractor.tokens_in == 900
        c = claims[0]
        assert c.extracted_by == "regex+llm" and "llm:anthropic/claude-haiku-5.5" in c.flags
        assert c.aav_usd is None  # the model invented an AAV the release never states; dropped
        assert c.term_years == 2 and c.start_season == "2027-28"
        _claims, _sources, _merged, seasons = normalize_all([story], [], teams)
        kochetkov = [s for s in seasons if s.season in ("2027-28", "2028-29")]
        assert len(kochetkov) >= 2  # regex and llm rows both describe the same two seasons
    finally:
        llm_fallback.register(None)
        llm_fallback.season_rows.clear()


def test_configure_from_env_is_a_no_op_without_key():
    assert llm_fallback.configure_from_env({}) is None and not llm_fallback.is_enabled()


def test_answers_are_cached_in_the_store(tmp_path, teams):
    from nhl_contracts.store import Store

    story = load_story("canes-sign-kochetkov")
    answer = [{"player_name": "Pyotr Kochetkov", "team_abbrev": "CAR", "contract_type": "extension", "term_years": 2}]
    store = Store(tmp_path / "s.sqlite")
    extractor = OpenRouterExtractor("test-key", transport=_fake_openrouter(answer), store=store)
    llm_fallback.register(extractor)
    try:
        extract_story(story, teams)
        extract_story(story, teams)
        assert extractor.calls == 1 and extractor.cache_hits == 1
        assert store.counts()["llm_cache"] == 1
        # the cache travels with the JSONL copies, so CI does not pay twice
        store.dump_jsonl(tmp_path / "raw")
        other = Store(tmp_path / "t.sqlite")
        other.load_jsonl(tmp_path / "raw")
        again = OpenRouterExtractor("test-key", transport=_fake_openrouter(answer), store=other)
        llm_fallback.register(again)
        extract_story(story, teams)
        assert again.calls == 0 and again.cache_hits == 1
    finally:
        llm_fallback.register(None)
        llm_fallback.season_rows.clear()


def test_contract_type_must_be_named_in_the_text(tmp_path, teams):
    from nhl_contracts.normalize.contracts import clean
    from nhl_contracts.store import Store

    story = load_story("leo-carlsson-signs-5-year-contract-with-ducks-match-flyers-offer-sheet")
    full = f"{clean(story.headline)}\n{clean(story.body)}"
    # a matched offer sheet: only a player without a contract can sign one
    row = {"player_name": "Leo Carlsson", "contract_type": "extension", "term_years": 5, "total_usd": 90_000_000}
    assert verify_row(row, full)["contract_type"] == "standard"
    assert verify_row({"player_name": "Pyotr Kochetkov", "contract_type": "extension", "term_years": 2},
                      KOCHETKOV_TEXT)["contract_type"] == "extension"
    # an extension the story implies without the word stays one
    implied = "Lucas Raymond signed an eight-year contract that begins next season."
    assert verify_row({"player_name": "Lucas Raymond", "contract_type": "extension", "term_years": 8},
                      implied)["contract_type"] == "extension"
    # an answer cached before the check is re-typed on read, without a call
    store = Store(tmp_path / "s.sqlite")
    extractor = OpenRouterExtractor("test-key", transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                                    store=store)
    key = extractor.cache_key(story, clean(story.body)[:12000])
    store.llm_cache_put(key, story.slug, extractor.model, [dict(row, flags=["llm:x"])], 0, 0, "2026-07-09")
    (cached,) = extractor(story)
    assert cached["contract_type"] == "standard" and extractor.calls == 0
