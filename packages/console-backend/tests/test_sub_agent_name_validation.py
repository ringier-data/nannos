"""A sub-agent name has to be an identifier, and the API is where that is enforced.

The name is not only a label: the orchestrator puts it in its task tool's enum, so it must
match ``BaseLocalSubAgentConfig.name`` in agent-common. Nothing checked it here, so a name
with a space was accepted at creation and only failed at chat time — inside the
orchestrator's registry build, where it took the owner's whole user record down with it.
"""

import pytest
from pydantic import ValidationError

from console_backend.models.scheduled_job import AutomatedSubAgentConfig
from console_backend.models.sub_agent import (
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
    validate_sub_agent_name,
)

ACCEPTED = [
    "data-analyst",
    "Alloy-AI-Assistant",
    "agent_v2",
    "A",
    "a" * 64,
]

REFUSED = [
    "Alloy AI Assistant",  # the dev incident
    "coffe agent",
    "",
    "   ",
    "3rd-party",  # must start with a letter
    "-leading-hyphen",
    "_leading-underscore",
    "has.a.dot",
    "slash/name",
    "a" * 65,
]


@pytest.mark.parametrize("name", ACCEPTED)
def test_validate_sub_agent_name_accepts(name):
    assert validate_sub_agent_name(name) == name


@pytest.mark.parametrize("name", REFUSED)
def test_validate_sub_agent_name_refuses(name):
    with pytest.raises(ValueError, match="Agent name must be"):
        validate_sub_agent_name(name)


def test_validate_sub_agent_name_trims():
    assert validate_sub_agent_name("  data-analyst  ") == "data-analyst"


def _create(name: str) -> SubAgentCreate:
    return SubAgentCreate(
        name=name,
        description="An agent.",
        type=SubAgentType.LOCAL,
        model="gpt-4o",
        system_prompt="Be useful.",
    )


def test_sub_agent_create_refuses_a_name_the_orchestrator_cannot_use():
    with pytest.raises(ValidationError, match="Agent name must be"):
        _create("Alloy AI Assistant")


def test_sub_agent_create_accepts_a_valid_name():
    assert _create("Alloy-AI-Assistant").name == "Alloy-AI-Assistant"


def test_sub_agent_update_refuses_a_bad_name():
    with pytest.raises(ValidationError, match="Agent name must be"):
        SubAgentUpdate(name="Renamed With Spaces")


def test_sub_agent_update_leaves_an_omitted_name_alone():
    """None means "do not touch the name" — it must not be treated as an empty one."""
    assert SubAgentUpdate(description="only the description").name is None


def _automated(name: str) -> AutomatedSubAgentConfig:
    return AutomatedSubAgentConfig(
        name=name,
        description="Runs on a schedule.",
        model_tier="standard",
        system_prompt="Be useful.",
    )


def test_automated_config_refuses_a_bad_name_at_the_request_boundary():
    """Checked here too, so the scheduler caller gets a 422 naming the field, not a 500
    from the SubAgentCreate that scheduler_service builds out of this."""
    with pytest.raises(ValidationError, match="Agent name must be"):
        _automated("Reminder Bot")


def test_automated_config_accepts_a_valid_name():
    assert _automated("reminder-bot").name == "reminder-bot"
