"""``GET /sub-agents/{id}?version=default``: the approved default version, by role, in one read.

A caller that wants "what a reviewer approved" used to read the record for its
``default_version`` number and then read that version — two reads per run for any agent
with a draft, and a default change able to land between them. Naming the version by
role resolves it in the same statement as the number.
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest

from console_backend.config import config
from console_backend.models.sub_agent import (
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
)
from tests.test_skill_reference_modes import _agent


@pytest.fixture
def prompt_limit(monkeypatch):
    def set_limit(n: int) -> None:
        monkeypatch.setattr(config.auto_approve, "max_system_prompt_length", n)

    return set_limit


@pytest.mark.asyncio
async def test_default_returns_the_approved_version_while_current_is_a_draft(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, _, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "approved-with-draft")
    prompt_limit(20)  # v2's prompt is over the limit: created, pending, not approved
    await svc.update_sub_agent(
        pg_session, agent_id, SubAgentUpdate(system_prompt="x" * 40), test_user_db
    )

    current = await svc.get_sub_agent_by_id(pg_session, agent_id)
    assert current.default_version == 1
    assert current.current_version == 2
    assert current.config_version.version == 2  # the draft, what the editor shows

    approved = await svc.get_sub_agent_by_id(pg_session, agent_id, version="default")
    assert approved.default_version == 1
    assert approved.current_version == 2
    assert approved.config_version.version == 1
    assert approved.config_version.system_prompt == "short prompt"

    by_number = await svc.get_sub_agent_by_id(pg_session, agent_id, version=1)
    assert by_number.config_version.id == approved.config_version.id


@pytest.mark.asyncio
async def test_default_on_an_agent_that_was_never_approved_embeds_no_version(
    wired, pg_session, test_user_db, prompt_limit
):
    """Only drafts exist: the record comes back, ``config_version`` does not."""
    svc, _, _ = wired
    prompt_limit(5)
    agent = await svc.create_sub_agent(
        pg_session,
        SubAgentCreate(
            name="never-approved",
            type=SubAgentType.LOCAL,
            description="long prompt, misses auto-approval",
            model="gpt-4o",
            system_prompt="a prompt longer than the limit",
            mcp_tools=[],
        ),
        test_user_db,
    )
    assert agent.default_version is None

    read = await svc.get_sub_agent_by_id(pg_session, agent.id, version="default")
    assert read is not None
    assert read.default_version is None
    assert read.config_version is None

    # The unqualified read still shows the draft, so the editor is unaffected.
    assert (
        await svc.get_sub_agent_by_id(pg_session, agent.id)
    ).config_version.version == 1
