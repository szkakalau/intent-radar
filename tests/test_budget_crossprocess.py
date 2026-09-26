"""The monthly budget must survive process boundaries.

Trap worth knowing (it cost a false negative once): LLM responses are cached to
``<data_dir>/cache/llm_cache.jsonl`` and **a cache hit never bills and never
counts**. Repeating the same prompt from a second process therefore *looks* like
"usage did not accumulate" even though persistence works fine. Every subprocess
below uses a unique prompt so each call is genuinely billable; the replay case is
asserted separately, because the fact that a cache hit is free is intentional
(the budget must never charge twice for one answer).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT, SRC

from intentradar.budget import BudgetGuard

WORKER = '''
import os
import sys

from intentradar.budget import BudgetGuard
from intentradar.config import Settings
from intentradar.llm import LLMConfig, NemotronClient

settings = Settings.from_env()
guard = BudgetGuard(path=settings.usage_path, monthly_budget_usd=settings.monthly_budget_usd)
client = NemotronClient(
    config=LLMConfig(api_key="", mock=True, cache_dir=settings.cache_dir),
    budget=guard,
)
response = client.complete("probe", os.environ["QA_PROMPT"])
print(f"llm_calls={guard.usage.llm_calls} cost={guard.usage.cost_usd:.8f} "
      f"cached={response.cached}")
'''


@pytest.fixture
def worker_script(tmp_path: Path) -> Path:
    """Write the child-process probe next to the isolated data dir."""
    path = tmp_path / "worker.py"
    path.write_text(WORKER, encoding="utf-8")
    return path


def _run_child(worker_script: Path, data_dir: Path, prompt: str) -> dict[str, str]:
    """Run one genuinely separate OS process and parse its report."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONPATH": str(SRC),
        "INTENTRADAR_DATA_DIR": str(data_dir),
        "INTENTRADAR_LLM_MOCK": "1",
        # Explicit rather than inherited: the USD estimate must not depend on
        # whatever the developer happens to have exported in their shell.
        "INTENTRADAR_ALLOW_UNPRICED": "1",
        "INTENTRADAR_MONTHLY_BUDGET_USD": "20",
        "QA_PROMPT": prompt,
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(worker_script)],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        timeout=120, check=True,
    )
    fields = dict(part.split("=", 1) for part in completed.stdout.strip().split())
    return fields


def test_llm_usage_accumulates_across_real_processes(
    worker_script: Path, tmp_path: Path
) -> None:
    """Three OS processes, three billable calls -> usage.json must read back 3."""
    data_dir = tmp_path / "data"

    reports = [
        _run_child(worker_script, data_dir, f"unique prompt number {i}")
        for i in range(1, 4)
    ]
    assert [r["llm_calls"] for r in reports] == ["1", "2", "3"], (
        f"each new process must add to the persisted tally, got {reports}"
    )

    # A brand-new guard reading only from disk must agree with the last child.
    guard = BudgetGuard(path=data_dir / "usage.json", monthly_budget_usd=20.0)
    assert guard.usage.llm_calls == 3
    assert guard.usage.cost_usd > 0, "usage must carry a cost, otherwise the budget is blind"
    assert guard.path.exists()


def test_a_repeated_prompt_is_served_from_cache_and_not_charged_twice(
    worker_script: Path, tmp_path: Path
) -> None:
    """Replaying a prompt must neither inflate usage nor double-bill."""
    data_dir = tmp_path / "data"
    first = _run_child(worker_script, data_dir, "the very same prompt")
    replay = _run_child(worker_script, data_dir, "the very same prompt")

    assert first["cached"] == "False"
    assert replay["cached"] == "True"
    assert replay["llm_calls"] == first["llm_calls"], "a cache hit must not be charged again"

    guard = BudgetGuard(path=data_dir / "usage.json", monthly_budget_usd=20.0)
    assert guard.usage.llm_calls == 1
