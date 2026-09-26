"""Shared pytest fixtures.

The default test run must be fully offline: every fixture redirects persistent
state into ``tmp_path`` and forces the deterministic mock LLM backend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTSET_DIR = REPO_ROOT / "data" / "testset"


@pytest.fixture(autouse=True)
def _offline_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force mock LLM and neutralise any ambient proxy/keys from the host."""
    monkeypatch.setenv("INTENTRADAR_LLM_MOCK", "1")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect INTENTRADAR_DATA_DIR to a temp dir so tests never touch the repo."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INTENTRADAR_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def src_dir() -> Path:
    """Path to src/ — used by source-scanning assertions."""
    return SRC


@pytest.fixture
def sample_watchlist(tmp_path: Path) -> Path:
    """A minimal valid watchlist (schema v2) written to disk."""
    payload = {
        "version": 2,
        "global": {
            "pages_per_sub": 2,
            "cache_max_age": "1d",
            "window_days": 3,
            "min_score": 5,
        },
        "projects": [
            {
                "name": "Einprag",
                "enabled": True,
                "site": "einprag.com",
                "subreddits": ["Anki"],
                "keywords": ["flashcard", "anki", "memorize"],
                "competitors": ["ankiapp", "quizlet", "brainscape"],
                "pain_words": [],
            }
        ],
    }
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
