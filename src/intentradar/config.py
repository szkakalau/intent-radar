"""Settings and watchlist loading, with fail-fast validation.

Read order: process env > repo-root ``.env`` (python-dotenv) > built-in default.
All paths resolve against ``INTENTRADAR_DATA_DIR`` (default ``<repo>/data``) and
**never** against the current working directory, so the CLI works from anywhere
after ``pip install -e .``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from intentradar.errors import ConfigError, MissingCredential

log = logging.getLogger(__name__)

WATCHLIST_SCHEMA_VERSION = 2

DEFAULT_NEBUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MAX_POSTS_PER_SOURCE = 200
DEFAULT_MAX_LLM_CALLS = 50
DEFAULT_MONTHLY_BUDGET_USD = 20.0


def _find_repo_root() -> Path:
    """Locate the repository root.

    Walks up from this file looking for ``pyproject.toml``; falls back to the
    current working directory when the package is installed outside a checkout.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"env {name}: expected int, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"env {name}: expected float, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"env {name}: expected bool, got {raw!r}")


def _env_mock(name: str) -> str:
    """``INTENTRADAR_LLM_MOCK``: ``auto`` / ``1`` / ``0``."""
    raw = _env_str(name, "auto").lower()
    if raw in {"", "auto"}:
        return "auto"
    if raw in {"1", "true", "yes", "on"}:
        return "1"
    if raw in {"0", "false", "no", "off"}:
        return "0"
    raise ConfigError(f"env {name}: expected auto|1|0, got {raw!r}")


@dataclass
class Settings:
    """Runtime settings resolved from env / .env / defaults."""

    repo_root: Path
    data_dir: Path
    watchlist_path: Path

    scrape_key: str = ""
    nebius_key: str = ""
    nebius_base_url: str = DEFAULT_NEBUS_BASE_URL
    model_everyday: str = ""
    model_reasoning: str = ""

    # Resolved LLM endpoint. The INTENTRADAR_LLM_* variables win over NEBIUS_*
    # so the semantic layer can be pointed at any OpenAI-compatible server
    # (Nebius Token Factory is the default, but the onboarding form does not
    # list every billing country, so a fallback endpoint must be possible).
    llm_base_url: str = DEFAULT_NEBUS_BASE_URL
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url_var: str = "NEBIUS_BASE_URL"
    llm_api_key_var: str = "NEBIUS_API_KEY"
    llm_model_var: str = "NEBIUS_MODEL_EVERYDAY"

    max_posts_per_source: int = DEFAULT_MAX_POSTS_PER_SOURCE
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS
    monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD

    llm_mock: str = "auto"  # "auto" | "1" | "0"
    allow_unpriced: bool = True

    # ── derived paths ──────────────────────────────────────────────────────
    @property
    def state_path(self) -> Path:
        """Path to the seen-id store."""
        return self.data_dir / "state.json"

    @property
    def usage_path(self) -> Path:
        """Path to the persisted monthly usage counters."""
        return self.data_dir / "usage.json"

    @property
    def cache_dir(self) -> Path:
        """Path to the on-disk LLM response cache."""
        return self.data_dir / "cache"

    @property
    def testset_dir(self) -> Path:
        """Root of the frozen evaluation datasets."""
        return self.data_dir / "testset"

    @property
    def reports_dir(self) -> Path:
        """Directory the daily reports are written to."""
        return self.data_dir / "reports"

    @property
    def mock_enabled(self) -> bool:
        """Whether the LLM runs against the deterministic mock backend."""
        if self.llm_mock == "1":
            return True
        if self.llm_mock == "0":
            return False
        return not self.llm_api_key

    @property
    def llm_endpoint(self) -> str:
        """The base URL the semantic layer will actually call."""
        return self.llm_base_url or DEFAULT_NEBUS_BASE_URL

    def describe_llm(self) -> dict[str, str]:
        """Resolved endpoint plus *which* env var supplied each value.

        Printed by ``config check`` — with two possible sources per setting,
        "why is it calling that URL?" has to be answerable without reading code.
        """
        return {
            "base_url": self.llm_endpoint,
            "base_url_from": self.llm_base_url_var,
            "api_key_from": self.llm_api_key_var,
            "model": self.llm_model,
            "model_from": self.llm_model_var,
            "mode": "mock" if self.mock_enabled else "live",
        }

    @classmethod
    def from_env(cls, root: Path | None = None) -> Settings:
        """Build settings from process env, then repo-root .env, then defaults."""
        repo_root = Path(root) if root else _find_repo_root()
        load_dotenv(repo_root / ".env", override=False)

        # INTENTRADAR_LLM_* overrides NEBIUS_*, value and provenance together.
        nebius_base_url = _env_str("NEBIUS_BASE_URL", DEFAULT_NEBUS_BASE_URL)
        override_base_url = _env_str("INTENTRADAR_LLM_BASE_URL")
        nebius_key = _env_str("NEBIUS_API_KEY")
        override_key = _env_str("INTENTRADAR_LLM_API_KEY")
        model_everyday = _env_str("NEBIUS_MODEL_EVERYDAY")
        override_model = _env_str("INTENTRADAR_LLM_MODEL")

        data_dir_raw = _env_str("INTENTRADAR_DATA_DIR")
        data_dir = Path(data_dir_raw) if data_dir_raw else repo_root / "data"
        data_dir = data_dir.expanduser()

        watchlist_raw = _env_str("INTENTRADAR_WATCHLIST")
        watchlist_path = (
            Path(watchlist_raw).expanduser()
            if watchlist_raw
            else (repo_root / "config" / "watchlist.json").expanduser()
        )

        return cls(
            repo_root=repo_root,
            data_dir=data_dir,
            watchlist_path=watchlist_path,
            scrape_key=_env_str("SCRAPECREATORS_API_KEY"),
            nebius_key=nebius_key,
            nebius_base_url=nebius_base_url,
            model_everyday=model_everyday,
            model_reasoning=_env_str("NEBIUS_MODEL_REASONING"),
            llm_base_url=override_base_url or nebius_base_url,
            llm_api_key=override_key or nebius_key,
            llm_model=override_model or model_everyday,
            llm_base_url_var=(
                "INTENTRADAR_LLM_BASE_URL" if override_base_url else "NEBIUS_BASE_URL"
            ),
            llm_api_key_var=(
                "INTENTRADAR_LLM_API_KEY" if override_key else "NEBIUS_API_KEY"
            ),
            llm_model_var=(
                "INTENTRADAR_LLM_MODEL" if override_model else "NEBIUS_MODEL_EVERYDAY"
            ),
            max_posts_per_source=_env_int(
                "INTENTRADAR_MAX_POSTS_PER_SOURCE", DEFAULT_MAX_POSTS_PER_SOURCE
            ),
            max_llm_calls=_env_int("INTENTRADAR_MAX_LLM_CALLS", DEFAULT_MAX_LLM_CALLS),
            monthly_budget_usd=_env_float(
                "INTENTRADAR_MONTHLY_BUDGET_USD", DEFAULT_MONTHLY_BUDGET_USD
            ),
            llm_mock=_env_mock("INTENTRADAR_LLM_MOCK"),
            allow_unpriced=_env_bool("INTENTRADAR_ALLOW_UNPRICED", True),
        )

    def require_scrape_key(self) -> str:
        """Return the ScrapeCreators key or raise a friendly MissingCredential."""
        if not self.scrape_key:
            raise MissingCredential(
                "SCRAPECREATORS_API_KEY",
                "copy .env.example to .env and fill it in",
            )
        return self.scrape_key

    def require_model(self, tier: str = "everyday") -> str:
        """Return the model slug for ``tier`` or explain how to obtain it."""
        slug = self.model_reasoning if tier == "reasoning" else self.llm_model
        if slug:
            return slug
        var = "NEBIUS_MODEL_REASONING" if tier == "reasoning" else self.llm_model_var
        raise ConfigError(
            f"env {var}: expected a real model slug, got empty — "
            "copy the exact slug from your provider's model list / playground "
            "(do not guess, the casing differs per model) and put it in .env"
        )


# ── watchlist ──────────────────────────────────────────────────────────────


def _require_str_list(container: dict[str, Any], path: str, key: str, *, allow_empty: bool) -> list[str]:
    """Validate a list[str] field and report the exact path on failure."""
    if key not in container:
        raise ConfigError(f"{path}.{key}: expected non-empty list[str], got missing")
    value = container[key]
    if not isinstance(value, list):
        raise ConfigError(f"{path}.{key}: expected non-empty list[str], got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{path}.{key}[{i}]: expected str, got {item!r}")
        out.append(item)
    if not out and not allow_empty:
        raise ConfigError(f"{path}.{key}: expected non-empty list[str], got []")
    return out


def _require_int(container: dict[str, Any], path: str, key: str, default: int) -> int:
    if key not in container:
        return default
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key}: expected int, got {type(value).__name__}")
    return value


def _require_str(container: dict[str, Any], path: str, key: str, default: str = "") -> str:
    if key not in container:
        return default
    value = container[key]
    if not isinstance(value, str):
        raise ConfigError(f"{path}.{key}: expected str, got {type(value).__name__}")
    return value


@dataclass
class ProjectConfig:
    """One monitored project."""

    name: str
    enabled: bool = True
    site: str = ""
    subreddits: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    # Reserved. rule_v3 scores with the module-level PAIN_WORDS constant and
    # deliberately ignores this field — enabling it would change the published
    # hit set, so it may only be switched on together with a judge version bump.
    pain_words: list[str] = field(default_factory=list)
    min_score: int | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Mapping view used by the verbatim v3 ``score()`` function."""
        return {"keywords": self.keywords, "competitors": self.competitors}


@dataclass
class GlobalConfig:
    """Settings shared by every project."""

    pages_per_sub: int = 2
    cache_max_age: str = "1d"
    window_days: int = 3
    min_score: int = 5


@dataclass
class Watchlist:
    """Parsed, validated ``config/watchlist.json``."""

    version: int
    global_config: GlobalConfig
    projects: list[ProjectConfig]

    def enabled_projects(self) -> list[ProjectConfig]:
        """Projects with ``enabled: true``, in file order."""
        return [p for p in self.projects if p.enabled]

    def get(self, name: str) -> ProjectConfig:
        """Return a project by name or raise ConfigError."""
        for p in self.projects:
            if p.name == name:
                return p
        raise ConfigError(f"watchlist.projects: no project named {name!r}")

    def min_score_for(self, project: ProjectConfig) -> int:
        """Per-project min_score, falling back to the global value."""
        return project.min_score if project.min_score is not None else self.global_config.min_score

    @staticmethod
    def load(path: Path) -> Watchlist:
        """Load and validate a watchlist file, failing before any work starts."""
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"watchlist: file not found at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"watchlist: invalid JSON at {path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"watchlist: expected object at root, got {type(raw).__name__}")

        version = raw.get("version")
        if version != WATCHLIST_SCHEMA_VERSION:
            raise ConfigError(
                f"watchlist.version: expected {WATCHLIST_SCHEMA_VERSION}, got {version!r}"
            )

        g_raw = raw.get("global")
        if not isinstance(g_raw, dict):
            raise ConfigError(
                f"watchlist.global: expected object, got {type(g_raw).__name__}"
            )
        g = GlobalConfig(
            pages_per_sub=_require_int(g_raw, "watchlist.global", "pages_per_sub", 2),
            cache_max_age=_require_str(g_raw, "watchlist.global", "cache_max_age", "1d"),
            window_days=_require_int(g_raw, "watchlist.global", "window_days", 3),
            min_score=_require_int(g_raw, "watchlist.global", "min_score", 5),
        )

        projects_raw = raw.get("projects")
        if not isinstance(projects_raw, list) or not projects_raw:
            raise ConfigError("watchlist.projects: expected non-empty list, got missing")

        projects: list[ProjectConfig] = []
        for i, p_raw in enumerate(projects_raw):
            path_str = f"watchlist.projects[{i}]"
            if not isinstance(p_raw, dict):
                raise ConfigError(f"{path_str}: expected object, got {type(p_raw).__name__}")
            name = _require_str(p_raw, path_str, "name")
            if not name:
                raise ConfigError(f"{path_str}.name: expected non-empty str, got missing")
            enabled = p_raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigError(f"{path_str}.enabled: expected bool, got {type(enabled).__name__}")
            min_score = p_raw.get("min_score")
            if min_score is not None and (isinstance(min_score, bool) or not isinstance(min_score, int)):
                raise ConfigError(
                    f"{path_str}.min_score: expected int, got {type(min_score).__name__}"
                )
            projects.append(
                ProjectConfig(
                    name=name,
                    enabled=enabled,
                    site=_require_str(p_raw, path_str, "site"),
                    subreddits=_require_str_list(p_raw, path_str, "subreddits", allow_empty=False),
                    keywords=_require_str_list(p_raw, path_str, "keywords", allow_empty=False),
                    competitors=_require_str_list(p_raw, path_str, "competitors", allow_empty=True),
                    pain_words=_require_str_list(p_raw, path_str, "pain_words", allow_empty=True),
                    min_score=min_score,
                )
            )

        log.debug("loaded watchlist v%s with %d projects", version, len(projects))
        return Watchlist(version=version, global_config=g, projects=projects)
