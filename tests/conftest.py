import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import build_fixtures  # noqa: E402


@pytest.fixture(scope="session")
def decks(tmp_path_factory):
    return build_fixtures.build_all(tmp_path_factory.mktemp("decks"))


@pytest.fixture(scope="session")
def sample(decks):
    return decks["sample"]


@pytest.fixture(scope="session")
def default_deck(decks):
    return decks["default"]
