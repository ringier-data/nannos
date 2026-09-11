"""The pass-ratio gate decides the session exit code, so it needs its own tests.

Deliberately pure — no pytest hooks, no LLM. The hooks in
tests/integration/conftest.py are a thin shell over EvalSession; the decisions
live here.
"""

from __future__ import annotations

import json

import pytest

from tests.support.eval_report import DEFAULT_MIN_PASS_RATIO, RATIO_ENV, EvalSession, min_pass_ratio


def _session(*outcomes: str, strict_failures: int = 0) -> EvalSession:
    session = EvalSession()
    for i, outcome in enumerate(outcomes):
        record = session.record_for(f"tests/integration/test_x.py::test_{i}")
        record.outcome = outcome
    for i in range(strict_failures):
        record = session.record_for(f"tests/integration/test_x.py::test_strict_{i}")
        record.outcome = "failed"
        record.strict = True
    return session


# ---------------------------------------------------------------------------
# Ratio
# ---------------------------------------------------------------------------


def test_ratio_above_threshold_tolerates_failures():
    """The whole point: one unlucky sample must not fail the run."""
    session = _session("passed", "passed", "passed", "failed")  # 75%

    assert session.pass_ratio == 0.75
    assert session.gate_failure_reason() is None


def test_ratio_below_threshold_fails_with_the_numbers():
    session = _session("passed", "failed", "failed", "failed")  # 25%

    reason = session.gate_failure_reason()
    assert reason is not None
    assert "25%" in reason and "1/4" in reason


def test_skips_do_not_count_as_evidence_either_way():
    """A skipped test says nothing about behaviour, so it must not dilute the
    ratio — otherwise skipping everything would produce a 0% failure."""
    session = _session("passed", "skipped", "skipped")

    assert len(session.judged) == 1
    assert session.pass_ratio == 1.0


def test_no_judged_tests_is_not_a_failure():
    """A run where everything skipped (no gateway) must not fail the gate."""
    session = _session("skipped", "skipped")

    assert session.pass_ratio == 0.0
    assert session.gate_failure_reason() is None


# ---------------------------------------------------------------------------
# Strict escape hatch
# ---------------------------------------------------------------------------


def test_strict_failure_fails_regardless_of_a_good_ratio():
    """Closes the hole in a pure ratio: a permanently-broken test would
    otherwise hide behind healthy siblings forever."""
    session = _session(*["passed"] * 20, strict_failures=1)  # ~95%

    assert session.pass_ratio > DEFAULT_MIN_PASS_RATIO
    reason = session.gate_failure_reason()
    assert reason is not None
    assert "strict" in reason


def test_strict_test_that_passes_does_not_trip_the_gate():
    session = EvalSession()
    record = session.record_for("tests/integration/test_x.py::test_important")
    record.outcome = "passed"
    record.strict = True

    assert session.gate_failure_reason() is None


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------


def test_threshold_is_overridable(monkeypatch):
    monkeypatch.setenv(RATIO_ENV, "0.5")
    assert min_pass_ratio() == 0.5

    session = _session("passed", "failed")  # 50%
    assert session.gate_failure_reason() is None


def test_unparseable_threshold_falls_back_to_the_default(monkeypatch):
    """A typo in CI config must not silently disable the gate."""
    monkeypatch.setenv(RATIO_ENV, "not-a-number")
    assert min_pass_ratio() == DEFAULT_MIN_PASS_RATIO


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_summary_reports_ratio_and_totals():
    session = EvalSession()
    record = session.record_for("tests/integration/test_x.py::test_slow")
    record.outcome = "passed"
    record.duration = 12.5
    record.input_tokens = 1000
    record.output_tokens = 200

    text = "\n".join(session.summary_lines())
    assert "test_slow" in text
    assert "12.5" in text
    assert "1,200" in text
    assert "100%" in text


def test_zero_tokens_is_labelled_not_reported():
    """A blank token column must not read as 'this run was free'."""
    session = _session("passed")

    text = "\n".join(session.summary_lines())
    assert "no token usage captured" in text


def test_artifact_written_only_when_requested(tmp_path, monkeypatch):
    session = _session("passed", "failed")

    monkeypatch.delenv("EVAL_REPORT_PATH", raising=False)
    assert session.write_artifact() is None

    target = tmp_path / "nested" / "report.json"
    monkeypatch.setenv("EVAL_REPORT_PATH", str(target))
    written = session.write_artifact()

    assert written == target
    payload = json.loads(target.read_text())
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["pass_ratio"] == 0.5
    assert len(payload["tests"]) == 2


def test_empty_session_produces_no_summary():
    """No integration tests ran — print nothing rather than an empty table."""
    assert EvalSession().summary_lines() == []


@pytest.mark.parametrize("outcome", ["passed", "failed"])
def test_records_accumulate_tokens_per_test(outcome):
    session = EvalSession()
    record = session.record_for("tests/integration/test_x.py::test_a")
    record.outcome = outcome
    record.input_tokens += 10
    record.output_tokens += 5
    record.input_tokens += 1

    assert record.total_tokens == 16
    assert session.total_tokens == 16


# ---------------------------------------------------------------------------
# Exit-status downgrade: what the ratio is allowed to absolve
# ---------------------------------------------------------------------------
# The ratio exists to tolerate one unlucky *sample*. It is not evidence about
# anything it did not sample, and downgrading a red run to green is the
# dangerous direction — so these pin exactly what may be absolved.

INTEGRATION = "tests/integration/test_x.py"


def test_a_sampled_failure_is_absolved():
    """The intended case: a tolerated flake, ratio met, run goes green."""
    session = _session("passed", "passed", "passed", "failed")  # 75%
    session.note_problem(f"{INTEGRATION}::test_3", "call")

    assert session.unaccounted_problems == []
    assert session.may_downgrade_exit_status() is True


def test_a_setup_error_is_not_absolved():
    """The reported bug: a fixture error never reaches the call phase.

    It is neither judged nor failed as far as the ratio is concerned, so a
    fixture regression erroring a whole module would go green while the
    surviving tests carried the ratio.
    """
    session = _session("passed", "passed", "passed", "passed")  # 100%
    session.note_problem(f"{INTEGRATION}::test_broken_fixture", "setup")

    assert session.unaccounted_problems == [f"{INTEGRATION}::test_broken_fixture"]
    assert session.gate_failure_reason() is None  # the ratio itself is happy
    assert session.may_downgrade_exit_status() is False


def test_a_teardown_error_is_not_absolved():
    """The call phase passed, so the record looks clean — the error is elsewhere."""
    session = _session("passed", "passed")
    session.note_problem(f"{INTEGRATION}::test_0", "teardown")

    assert session.unaccounted_problems == [f"{INTEGRATION}::test_0"]
    assert session.may_downgrade_exit_status() is False


def test_unit_failures_in_a_mixed_run_are_not_absolved():
    """The second reported bug.

    A mixed run (RUN_INTEGRATION_TESTS=1 with a broad -m) judges integration
    tests while unit tests also run. Unit failures are deterministic, not
    sampling noise, and the ratio has no business speaking for them.
    """
    session = _session("passed", "passed", "passed", "passed")
    session.note_problem("tests/test_config.py::test_defaults", "call")

    assert session.unaccounted_problems == ["tests/test_config.py::test_defaults"]
    assert session.may_downgrade_exit_status() is False


def test_strict_failures_are_not_absolved_here_either():
    """Redundant with gate_failure_reason, deliberately: two reasons to stay red."""
    session = _session("passed", "passed", "passed", strict_failures=1)
    session.note_problem(f"{INTEGRATION}::test_strict_0", "call")

    assert session.unaccounted_problems == [f"{INTEGRATION}::test_strict_0"]
    assert session.may_downgrade_exit_status() is False


def test_a_bad_ratio_blocks_the_downgrade_even_with_nothing_unaccounted():
    session = _session("passed", "failed", "failed", "failed")  # 25%
    for i in (1, 2, 3):
        session.note_problem(f"{INTEGRATION}::test_{i}", "call")

    assert session.unaccounted_problems == []
    assert session.may_downgrade_exit_status() is False


def test_the_first_phase_seen_wins():
    """A setup error is a more useful label than the teardown noise after it."""
    session = _session("passed")
    session.note_problem(f"{INTEGRATION}::test_a", "setup")
    session.note_problem(f"{INTEGRATION}::test_a", "teardown")

    assert session.problems[f"{INTEGRATION}::test_a"] == "setup"


def test_unaccounted_problems_are_reported_in_the_artifact(tmp_path, monkeypatch):
    """CI trends the JSON, so the reason a run stayed red must be in it."""
    monkeypatch.setenv("EVAL_REPORT_PATH", str(tmp_path / "report.json"))
    session = _session("passed", "passed")
    session.note_problem(f"{INTEGRATION}::test_broken", "setup")

    path = session.write_artifact()
    assert path is not None
    payload = json.loads(path.read_text())
    assert payload["unaccounted_problems"] == [f"{INTEGRATION}::test_broken"]
