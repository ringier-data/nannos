"""The notify-only rule for `prompt`, enforced where the row is written.

On a watch with a sub-agent `prompt` instructs the agent; on a notify-only watch it is
the brief the written notification follows. A verbatim `notification_message` leaves the
brief nothing to do, and keeping it inert is how an old agent instruction comes back as a
brief the moment the message is emptied — so it is cleared, for every writer of the row,
not only the console forms.
"""

from console_backend.models.scheduled_job import JobType
from console_backend.services.scheduler_service import effective_prompt


def test_a_verbatim_message_on_a_notify_only_watch_clears_the_prompt():
    assert effective_prompt(JobType.WATCH, None, "Say only DOWN", "Email the owner") is None


def test_an_empty_message_keeps_the_brief():
    assert effective_prompt(JobType.WATCH, None, "", "Link each item") == "Link each item"
    assert effective_prompt(JobType.WATCH, None, "   ", "Link each item") == "Link each item"
    assert effective_prompt(JobType.WATCH, None, None, "Link each item") == "Link each item"


def test_an_agent_keeps_its_instruction_whatever_the_message_says():
    assert effective_prompt(JobType.WATCH, 7, "leftover text", "Escalate to ops") == "Escalate to ops"


def test_tasks_are_not_touched():
    assert effective_prompt(JobType.TASK, 7, "irrelevant", "Do the thing") == "Do the thing"
    assert effective_prompt("task", None, "irrelevant", "Do the thing") == "Do the thing"
