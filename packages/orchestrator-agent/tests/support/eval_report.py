"""Pass-ratio gating and per-test cost reporting for the non-deterministic tier.

Two problems this solves, both specific to tests that call a real LLM.

**Gating.** A single unlucky sample should not fail a run. The usual fix is a
rerun plugin (``--reruns``), which retries a failing test until it passes — that
*hides* flakiness rather than measuring it, and tells you nothing about how often
the behaviour actually holds. Instead every test runs exactly once and the
session is judged on the aggregate pass ratio.

The obvious hole in a pure ratio is that a permanently-broken test disappears
into it: nine flaky-but-passing tests carry one that fails every single run, and
90% still clears a 75% gate. Hence ``@pytest.mark.strict`` — marked tests must
pass, and one failure fails the session no matter what the ratio says. Use it for
behaviour that is not supposed to be probabilistic.

**Cost.** Execution time and token usage per test, which the ticket asks for and
which is otherwise invisible: a scenario that quietly costs 40k tokens looks the
same as one costing 400 until someone reads a bill.

Both are reported in the terminal summary, and written to a JSON artifact so a CI
job can trend them instead of scrolling past.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MIN_PASS_RATIO = 0.75
ARTIFACT_ENV = "EVAL_REPORT_PATH"
RATIO_ENV = "EVAL_MIN_PASS_RATIO"


def min_pass_ratio() -> float:
    """Required aggregate pass ratio, overridable per run."""
    try:
        return float(os.getenv(RATIO_ENV, str(DEFAULT_MIN_PASS_RATIO)))
    except ValueError:
        return DEFAULT_MIN_PASS_RATIO


@dataclass
class TestRecord:
    nodeid: str
    outcome: str = "unknown"
    duration: float = 0.0
    strict: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class EvalSession:
    """Accumulates results for the tests under gating. One per pytest session."""

    records: dict[str, TestRecord] = field(default_factory=dict)

    def record_for(self, nodeid: str) -> TestRecord:
        return self.records.setdefault(nodeid, TestRecord(nodeid=nodeid))

    # -- Aggregates ---------------------------------------------------------

    @property
    def judged(self) -> list[TestRecord]:
        """Records that count toward the ratio — skips are not evidence either way."""
        return [r for r in self.records.values() if r.outcome in ("passed", "failed")]

    @property
    def passed(self) -> list[TestRecord]:
        return [r for r in self.judged if r.outcome == "passed"]

    @property
    def failed(self) -> list[TestRecord]:
        return [r for r in self.judged if r.outcome == "failed"]

    @property
    def strict_failures(self) -> list[TestRecord]:
        return [r for r in self.failed if r.strict]

    @property
    def pass_ratio(self) -> float:
        judged = self.judged
        return len(self.passed) / len(judged) if judged else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.records.values())

    @property
    def total_duration(self) -> float:
        return sum(r.duration for r in self.records.values())

    def gate_failure_reason(self) -> str | None:
        """Why the session should fail, or None if it passes the gate.

        A strict failure always fails, regardless of ratio. Otherwise the
        aggregate ratio decides.
        """
        if self.strict_failures:
            names = ", ".join(r.nodeid.split("::")[-1] for r in self.strict_failures)
            return f"{len(self.strict_failures)} strict test(s) failed: {names}"

        if not self.judged:
            return None

        threshold = min_pass_ratio()
        if self.pass_ratio < threshold:
            return (
                f"pass ratio {self.pass_ratio:.0%} is below the required {threshold:.0%} "
                f"({len(self.passed)}/{len(self.judged)} passed)"
            )
        return None

    # -- Output -------------------------------------------------------------

    def summary_lines(self) -> list[str]:
        """Human-readable report for the terminal summary."""
        if not self.records:
            return []

        lines = [
            f"{'test':<58} {'outcome':<8} {'secs':>7} {'tokens':>9}",
            "-" * 85,
        ]
        for record in sorted(self.records.values(), key=lambda r: -r.duration):
            name = record.nodeid.split("::", 1)[-1]
            if len(name) > 57:
                name = "…" + name[-56:]
            tokens = f"{record.total_tokens:,}" if record.total_tokens else "-"
            flag = "!" if record.strict else " "
            lines.append(f"{name:<58}{flag}{record.outcome:<8} {record.duration:>7.1f} {tokens:>9}")

        lines.append("-" * 85)
        judged = self.judged
        if judged:
            lines.append(
                f"pass ratio {self.pass_ratio:.0%} ({len(self.passed)}/{len(judged)}), "
                f"threshold {min_pass_ratio():.0%}  |  "
                f"{self.total_duration:.0f}s  |  {self.total_tokens:,} tokens"
            )
        # Token totals are only as complete as the callback coverage; say so
        # rather than letting a zero read as "this run was free".
        if self.total_tokens == 0:
            lines.append("(no token usage captured — the provider may not report usage_metadata)")
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass_ratio": self.pass_ratio,
            "threshold": min_pass_ratio(),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "judged": len(self.judged),
            "total_duration_seconds": round(self.total_duration, 2),
            "total_tokens": self.total_tokens,
            "tests": [
                {
                    "nodeid": r.nodeid,
                    "outcome": r.outcome,
                    "duration_seconds": round(r.duration, 3),
                    "strict": r.strict,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                }
                for r in self.records.values()
            ],
        }

    def write_artifact(self) -> Path | None:
        """Write the JSON report when EVAL_REPORT_PATH is set."""
        target = os.getenv(ARTIFACT_ENV)
        if not target or not self.records:
            return None
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2))
        return path
