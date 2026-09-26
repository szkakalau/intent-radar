"""Exception hierarchy.

Every error raised by IntentRadar derives from `IntentRadarError` so the CLI can
map it to a single exit-code table:

    0  success
    1  evaluation mismatch (recomputed numbers differ from the published ones)
    2  ConfigError / MissingCredential
    3  BudgetExceeded / GateExceeded
    4  ProviderError
"""

from __future__ import annotations


class IntentRadarError(Exception):
    """Base class for all IntentRadar errors."""

    exit_code: int = 1

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class ConfigError(IntentRadarError):
    """Configuration is missing, malformed, or of the wrong type.

    Messages are always human-readable and name the exact field, e.g.
    ``watchlist.projects[1].keywords: expected non-empty list[str], got missing``.
    """

    exit_code = 2


class MissingCredential(IntentRadarError):
    """An API key is required for this operation but was not provided."""

    exit_code = 2

    def __init__(self, env_var: str, hint: str = "") -> None:
        self.env_var = env_var
        self.hint = hint
        message = f"missing credential: set {env_var} in your environment or in .env"
        if hint:
            message = f"{message} — {hint}"
        super().__init__(message)


class ProviderError(IntentRadarError):
    """A data-source or LLM provider call failed after retries."""

    exit_code = 4

    def __init__(self, provider: str, detail: str = "") -> None:
        self.provider = provider
        self.detail = detail
        message = f"{provider} call failed"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class GateExceeded(IntentRadarError):
    """A per-run gate (`MAX_LLM_CALLS`) was hit. Hard stop."""

    exit_code = 3

    def __init__(self, gate: str, limit: int, detail: str = "") -> None:
        self.gate = gate
        self.limit = limit
        self.detail = detail
        message = f"gate exceeded: {gate} limit {limit} reached"
        if detail:
            message = f"{message} — {detail}"
        super().__init__(message)


class BudgetExceeded(IntentRadarError):
    """The monthly USD budget is exhausted (>= 100%). Refuse to run."""

    exit_code = 3

    def __init__(self, used_usd: float, budget_usd: float, detail: str = "") -> None:
        self.used_usd = used_usd
        self.budget_usd = budget_usd
        self.detail = detail
        ratio = (used_usd / budget_usd * 100) if budget_usd else 0.0
        message = (
            f"monthly budget exhausted: ${used_usd:.2f} / ${budget_usd:.2f} used ({ratio:.0f}%)"
        )
        if detail:
            message = f"{message} — {detail}"
        super().__init__(message)
