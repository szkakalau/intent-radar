"""Budget + gate tests (P0-5): hard stop, 90% warn, 100% block, cross-process."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from intentradar.budget import SCRAPE_CREDIT_USD, BudgetGuard, BudgetState, RunGates, Usage
from intentradar.errors import BudgetExceeded, GateExceeded


def _resp(prompt_tokens: int = 100, completion_tokens: int = 50, cost_usd: float = 0.01):
    """Minimal stand-in for LLMResponse (duck-typed on purpose)."""
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )


def test_max_llm_calls_hard_stops_on_the_next_call(tmp_data_dir: Path) -> None:
    """MAX_LLM_CALLS=3 → the 4th call raises GateExceeded (exit code 3)."""
    gates = RunGates(max_llm_calls=3)
    for _ in range(3):
        gates.count_llm_call()
    with pytest.raises(GateExceeded) as excinfo:
        gates.count_llm_call()
    assert excinfo.value.exit_code == 3
    assert "MAX_LLM_CALLS" in excinfo.value.message


def test_max_posts_per_source_is_a_soft_cap(tmp_data_dir: Path) -> None:
    """The post cap returns False (stop paginating) but never aborts the run."""
    gates = RunGates(max_posts_per_source=2)
    assert gates.count_post() is True
    assert gates.count_post() is True
    assert gates.count_post() is False
    assert gates.posts_seen == 3


def test_usage_accumulates_and_persists(tmp_data_dir: Path) -> None:
    """Usage is written to disk and visible to a second process/instance."""
    path = tmp_data_dir / "usage.json"
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    guard.record_llm(_resp())
    guard.record_llm(_resp(cost_usd=0.02))
    guard.record_credits(10)

    assert guard.usage.llm_calls == 2
    assert guard.usage.cost_usd == pytest.approx(0.03)
    assert guard.usage.scrape_credits == 10

    # Simulate another process picking the file up.
    second = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert second.usage.llm_calls == 2
    assert second.usage.cost_usd == pytest.approx(0.03)
    assert second.usage.prompt_tokens == 200


def test_usage_accumulates_across_real_processes(tmp_data_dir: Path) -> None:
    """A genuinely separate interpreter must see the same counters."""
    path = tmp_data_dir / "usage.json"
    snippet = (
        "from intentradar.budget import BudgetGuard\n"
        "from types import SimpleNamespace\n"
        f"g = BudgetGuard(path=r'{path}', monthly_budget_usd=20.0)\n"
        "g.record_llm(SimpleNamespace(prompt_tokens=10, completion_tokens=5, cost_usd=0.5))\n"
        "print(g.usage.llm_calls, g.usage.cost_usd)\n"
    )
    for _ in range(2):
        out = subprocess.run(
            [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
        )
        assert out.returncode == 0
    final = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert final.usage.llm_calls == 2
    assert final.usage.cost_usd == pytest.approx(1.0)


def test_90_percent_warns(tmp_data_dir: Path) -> None:
    """Hand-editing usage.json to 91% yields WARN, not a block."""
    path = tmp_data_dir / "usage.json"
    path.write_text(
        json.dumps({"month": Usage.current_month(), "cost_usd": 18.2, "llm_calls": 4}),
        encoding="utf-8",
    )
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert guard.check() is BudgetState.WARN
    assert guard.require_ok() is BudgetState.WARN


def test_100_percent_refuses_to_run(tmp_data_dir: Path) -> None:
    """At >= 100% the guard blocks and require_ok raises."""
    path = tmp_data_dir / "usage.json"
    path.write_text(
        json.dumps({"month": Usage.current_month(), "cost_usd": 20.0, "llm_calls": 9}),
        encoding="utf-8",
    )
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert guard.check() is BudgetState.BLOCKED
    with pytest.raises(BudgetExceeded) as excinfo:
        guard.require_ok()
    assert excinfo.value.exit_code == 3


def test_scrape_credits_count_toward_the_budget(tmp_data_dir: Path) -> None:
    """Collection spend is converted to USD, so the breaker actually sees it.

    Regression: with 100,000 credits burned and $0 of LLM spend, the breaker used
    to report 0% and happily keep going.
    """
    path = tmp_data_dir / "usage.json"
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    guard.record_credits(100_000)

    assert guard.usage.cost_usd == 0.0
    assert guard.scrape_cost_usd == pytest.approx(100_000 * SCRAPE_CREDIT_USD)
    assert guard.effective_cost_usd == pytest.approx(188.0)
    assert guard.ratio >= 1.0
    assert guard.check() is BudgetState.BLOCKED
    with pytest.raises(BudgetExceeded):
        guard.require_ok()


def test_effective_cost_is_the_sum_of_both_sides(tmp_data_dir: Path) -> None:
    """LLM cost and collection cost are added before the ratio is computed."""
    path = tmp_data_dir / "usage.json"
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    guard.record_llm(_resp(cost_usd=2.0))
    guard.record_credits(1_000)  # ≈ $1.88

    assert guard.effective_cost_usd == pytest.approx(2.0 + 1_000 * SCRAPE_CREDIT_USD)
    assert guard.ratio == pytest.approx((2.0 + 1.88) / 20.0, abs=1e-3)
    assert guard.check() is BudgetState.OK
    assert "credits" in guard.summary()


def test_credit_conversion_is_a_real_number(tmp_data_dir: Path) -> None:
    """$47 per 25,000 credits, and never zero (a zero price disables the gate)."""
    assert SCRAPE_CREDIT_USD == pytest.approx(0.00188, abs=1e-6)
    assert SCRAPE_CREDIT_USD > 0


def test_month_rollover_resets_counters(tmp_data_dir: Path) -> None:
    """A stale month in usage.json is reset instead of blocking forever."""
    path = tmp_data_dir / "usage.json"
    path.write_text(
        json.dumps({"month": "1999-01", "cost_usd": 999.0, "llm_calls": 42}),
        encoding="utf-8",
    )
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert guard.usage.month == Usage.current_month()
    assert guard.usage.cost_usd == 0.0
    assert guard.usage.llm_calls == 0


def test_corrupt_usage_file_starts_fresh(tmp_data_dir: Path) -> None:
    """A torn/unreadable usage file must not brick the tool."""
    path = tmp_data_dir / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    assert guard.usage.cost_usd == 0.0


def test_atomic_write_leaves_no_tmp_file(tmp_data_dir: Path) -> None:
    """Writes go through .tmp + os.replace; no leftover temp files."""
    path = tmp_data_dir / "usage.json"
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    guard.record_credits(3)
    assert path.exists()
    assert not list(tmp_data_dir.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["scrape_credits"] == 3


def test_reset_zeroes_usage(tmp_data_dir: Path) -> None:
    """`intentradar budget --reset` returns the counters to zero."""
    path = tmp_data_dir / "usage.json"
    guard = BudgetGuard(path=path, monthly_budget_usd=20.0)
    guard.record_llm(_resp(cost_usd=5.0))
    guard.reset()
    assert guard.usage.cost_usd == 0.0
    assert BudgetGuard(path=path, monthly_budget_usd=20.0).usage.cost_usd == 0.0
