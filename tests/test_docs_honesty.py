"""Format discipline for published numbers: a rate is never printed alone.

Why this file exists
--------------------
At n=9, "precision 100.0%" reads as *"no false positives"* while the 95% Wilson
lower bound is 70%. A bare percentage is exactly what we accuse other tools of
publishing, so the rule is not a paragraph of warnings that can be scrolled
past — it is a format rule, and it is enforced here.

The rule: any line of prose that states a precision / recall / F1 / noise
figure must carry its raw counts ``(x/y)`` **and** its interval on the *same
line*. Verbatim tool output inside fenced blocks is exempt: it is reproduced
byte-for-byte from the tool, which already prints counts and CI (on adjacent
lines, as its own table format dictates).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT

README = REPO_ROOT / "README.md"

_RATE_WORD = re.compile(r"\b(precision|recall|noise rate|noise|F1)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\d+(?:\.\d+)?%")
_COUNTS = re.compile(r"\(\d+\s*/\s*\d+\)")
_INTERVAL = re.compile(r"95%\s*CI|\[\d+%,\s*\d+%\]")


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """Lines outside fenced code blocks, with their 1-based line numbers."""
    out: list[tuple[int, str]] = []
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = not inside
            continue
        if not inside:
            out.append((number, line))
    return out


def _offenders(text: str) -> list[tuple[int, str]]:
    """Prose lines that quote a rate without both its counts and its interval."""
    bad: list[tuple[int, str]] = []
    for number, line in _prose_lines(text):
        if not _RATE_WORD.search(line) or not _PERCENT.search(line):
            continue
        if _COUNTS.search(line) and _INTERVAL.search(line):
            continue
        bad.append((number, line.strip()))
    return bad


@pytest.mark.skipif(not README.exists(), reason="README.md not present")
def test_readme_never_prints_a_rate_alone() -> None:
    """Every precision / recall / F1 / noise figure carries counts + interval."""
    offenders = _offenders(README.read_text(encoding="utf-8"))
    detail = "\n".join(f"  line {n}: {text}" for n, text in offenders)
    assert not offenders, (
        "a published rate must carry its counts (x/y) and its 95% CI on the SAME "
        "line — a bare percentage reads as more certain than it is:\n" + detail
    )


@pytest.mark.skipif(not README.exists(), reason="README.md not present")
def test_readme_names_the_model_next_to_the_rates() -> None:
    """The headline numbers must not be separable from the model that produced them.

    A screenshot of "100.0%" with the model name three paragraphs away becomes
    "IntentRadar is 100% accurate". The model goes in the same block as the
    rates.
    """
    text = README.read_text(encoding="utf-8")
    head = text.split("## Quickstart", 1)[0].lower()
    assert "deepseek-v4-flash" in head, (
        "the status block must name the model beside the rates"
    )
    assert "nemotron" in head, (
        "the status block must state that these are NOT Nemotron figures"
    )


@pytest.mark.skipif(not README.exists(), reason="README.md not present")
def test_readme_states_the_audit_is_einprag_only() -> None:
    """Bootstrap is not reverse-audited yet; the page must not imply otherwise."""
    text = README.read_text(encoding="utf-8").lower()
    assert "einprag only" in text or "einprag-only" in text


def test_the_scanner_catches_a_bare_rate() -> None:
    """The guard must actually fail on the thing it is guarding against."""
    sample = (
        "our pipeline reaches 95.0% precision on this testset\n"
        "precision 90.0% (9/10), 95% CI [60%, 98%]  <- fine\n"
    )
    offenders = _offenders(sample)
    assert len(offenders) == 1
    assert "95.0% precision" in offenders[0][1]


def test_the_scanner_ignores_verbatim_tool_output() -> None:
    """Fenced blocks are reproduced byte-for-byte; they are not ours to reformat."""
    sample = "```\nprecision    100.0%\nrecall        90.0%\n```\n"
    assert _offenders(sample) == []


def test_prose_lines_skips_fenced_blocks() -> None:
    """Guarding the fenced-block logic itself, or the two tests above are vacuous."""
    sample = "a\n```\nskipped\n```\nb\n"
    assert [line for _, line in _prose_lines(sample)] == ["a", "b"]


# Documents that publish figures AS CURRENT CLAIMS. These are held to the
# format rule, because they are what a reader (or a screenshot) will quote.
_PUBLISHING_DOCS = ("README.md",)

# Documents exempt from the rule, with the reason recorded here rather than
# silently skipped. SIGNIFICANT_UPDATES.md is a historical ledger: several of
# its rows were written before the tool printed intervals, and back-filling
# intervals onto a superseded run would be inventing them. It carries a header
# saying so.
#
# THE EXEMPTION IS PER-FILE, NOT PER-NUMBER. It covers the ledger and nothing
# else. A figure quoted out of the ledger into the README or any outward-facing
# page loses the exemption and must carry counts + interval (or state counts
# alone). That rule is restated in the ledger's own header, and the test below
# only exempts while that header is present — so the exemption cannot quietly
# widen into "any figure may be bare as long as it originated here".
_EXEMPT_DOCS = {
    "SIGNIFICANT_UPDATES.md": "historical ledger; figures quoted as first printed",
}


def test_docs_guard_covers_every_publishing_markdown_file() -> None:
    """The rule is repo-wide, so the check is too — not a README special case.

    Any NEW markdown file that states a rate is caught automatically; exempting
    one requires adding it to :data:`_EXEMPT_DOCS` *with a reason*, so the
    exemption is visible in review instead of buried in a condition.
    """
    for path in sorted(Path(REPO_ROOT).glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.name in _EXEMPT_DOCS:
            assert "historical ledger" in text.lower(), (
                f"{path.name} is exempt but no longer carries its disclaimer header"
            )
            continue
        offenders = _offenders(text)
        detail = "\n".join(f"  {path.name}:{n}: {t}" for n, t in offenders)
        assert not offenders, f"bare rate in {path.name}:\n{detail}"

    for name in _PUBLISHING_DOCS:
        assert (Path(REPO_ROOT) / name).exists(), f"{name} missing — the guard is vacuous"


def test_the_exemption_does_not_travel_with_a_number() -> None:
    """A figure leaving the ledger loses the exemption — pinned both ways.

    Otherwise someone pastes a historical row into the README, the ledger is
    exempt so nothing complains, and a bare percentage becomes a public claim.
    """
    ledger = REPO_ROOT / "SIGNIFICANT_UPDATES.md"
    header = ledger.read_text(encoding="utf-8").lower()
    assert "historical ledger" in header
    assert "the exemption is per-file, not per-number" in header, (
        "the ledger no longer states that its exemption stops at its own border"
    )
    assert "loses its exemption" in header or "leaves this file leaves" in header

    # And the mechanism itself: a rate in a NON-exempt file is still caught.
    offenders = _offenders("README.md content claiming 91.0% recall on einprag\n")
    assert len(offenders) == 1
