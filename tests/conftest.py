import json
from pathlib import Path

import pytest

from nhl_contracts.config import STATIC_TEAMS
from nhl_contracts.store import Story

FIXTURES = Path(__file__).parent / "fixtures"
FORGE = FIXTURES / "forge"


def load_story(slug_prefix: str) -> Story:
    matches = sorted(p for p in FORGE.glob("*.json") if p.name.startswith(slug_prefix))
    assert matches, f"no fixture starting with {slug_prefix}"
    return Story.from_json(json.loads(matches[0].read_text(encoding="utf-8")))


def all_stories() -> list[Story]:
    return [Story.from_json(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(FORGE.glob("*.json"))]


@pytest.fixture(scope="session")
def teams() -> dict[int, str]:
    return dict(STATIC_TEAMS)


@pytest.fixture(scope="session")
def stories() -> list[Story]:
    return all_stories()


@pytest.fixture(scope="session")
def espn_payload() -> dict:
    return json.loads((FIXTURES / "espn" / "transactions-2026-06-25-2026-07-15.json").read_text(encoding="utf-8"))
