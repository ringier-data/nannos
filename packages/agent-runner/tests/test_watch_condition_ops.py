"""The runner knows nothing about watch jobs.

It used to call the check tool, compare the result and decide whether to act. Then it
kept a verdict check and wrote the notification text. Both are the scheduler's now: it
owns whether a job acts and what it says, and dispatches a plain prompt like any other
job. What is left here is "run this sub-agent" or "deliver this text".

These tests guard that absence — a watch-shaped branch reappearing here is how the two
services would start disagreeing about whether a job should have fired.
"""

import inspect

import agent.core as core


def test_no_condition_evaluation_remains():
    for name in ("_evaluate_watch", "_apply_condition_op", "_is_empty"):
        assert not hasattr(core.AgentRunner, name), f"{name} is back"
        assert name not in vars(core), f"{name} is back"


def test_no_notification_writing_remains():
    # Writing the message needs the check result and a model; the scheduler has both, and
    # doing it here meant a triggered watch had to round-trip through an agent run.
    assert not hasattr(core.AgentRunner, "_generate_watch_message")


def test_the_watch_conditions_package_is_not_imported():
    source = inspect.getsource(core)
    assert "watch_conditions" not in source
    assert "jsonpath" not in source


def test_the_dispatch_is_not_read_for_watch_specifics():
    source = inspect.getsource(core.AgentRunner._stream_impl)
    for gone in ('message_meta.get("watch")', 'job_type == "watch"', "last_check_result"):
        assert gone not in source, f"{gone} is back"


def test_a_dispatch_without_an_agent_delivers_its_text():
    # The notification path: nothing to run, so the text the scheduler wrote is echoed
    # back for the delivery channel to pick up.
    source = inspect.getsource(core.AgentRunner._stream_impl)
    assert "agent_message or message_text" in source
