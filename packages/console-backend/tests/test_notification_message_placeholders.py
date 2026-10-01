"""A fixed notification text is delivered verbatim, so a placeholder in it is refused.

Nothing renders `notification_message`: the engine sends it as stored. A draft that wrote
`New bug report filed: {{title}} ({{id}})` would have delivered those braces on every
trigger. The drafting endpoint turns such a text into a brief; these are the save-side
rule every other writer — the form, the task-scheduler agent over MCP — meets.
"""

import pytest
from pydantic import ValidationError

from console_backend.models.scheduled_job import ScheduledJobCreate, ScheduledJobUpdate, find_placeholder

WATCH = {
    "job_type": "watch",
    "name": "New bugs",
    "schedule_kind": "interval",
    "interval_seconds": 300,
    "check_tool": "console_list_bug_reports",
    "cel_expr": "result.reports",
}


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("New bug report filed: {{title}} ({{id}})", "{{title}}"),
        ("{{ title }}", "{{ title }}"),
        ("Cost is ${amount}", "${amount}"),
        ("Report {id} was filed", "{id}"),
        ("Report {report.id} was filed", "{report.id}"),
        ("Pull request #123 has been merged", None),
        ("An empty {} is not a placeholder", None),
        ('A JSON body {"a": 1} is not one either', None),
        ("", None),
        (None, None),
    ],
)
def test_find_placeholder(text, found):
    assert find_placeholder(text) == found


def test_create_refuses_a_placeholder_and_says_what_to_do_instead():
    with pytest.raises(ValidationError) as exc:
        ScheduledJobCreate(**WATCH, notification_message="New bug report filed: {{title}}")

    message = str(exc.value)
    assert "{{title}}" in message
    assert "prompt" in message


def test_create_accepts_plain_fixed_text():
    job = ScheduledJobCreate(**WATCH, notification_message="A new bug report was filed")

    assert job.notification_message == "A new bug report was filed"


def test_update_parses_a_placeholder_and_leaves_the_rule_to_the_service():
    # A stored job's text is resent by every save; refusing it at parse time would refuse
    # a reader their own delivery change. The service checks a changed text only.
    assert ScheduledJobUpdate(notification_message="Report {id} was filed").notification_message


def test_non_ascii_names_are_not_placeholders_as_in_the_form():
    # The form's JS pattern is ASCII-only; the two must agree on what is refused.
    assert find_placeholder("Größe: {größe}") is None


def test_update_can_still_clear_the_text():
    assert ScheduledJobUpdate(notification_message=None).notification_message is None
    # An empty text is a clear, as null is (see test_blank_text_is_stored_as_empty).
    assert ScheduledJobUpdate(notification_message="").notification_message is None


def test_blank_text_is_stored_as_empty():
    # The engine reads these by truthiness: a whitespace-only value would be a blank
    # message sent, a judge run on nothing, a blank agent instruction.
    job = ScheduledJobCreate(**{**WATCH, "llm_condition": "   "}, notification_message="  ", prompt=" \n")

    assert job.notification_message == ""
    assert job.prompt == ""
    assert job.llm_condition is None
    update = ScheduledJobUpdate(notification_message="  ", llm_condition="\t", prompt=" ")
    assert (update.notification_message, update.llm_condition, update.prompt) == (None, None, None)
