"""Unit tests for ``_build_subagent_resume_command``.

Covers the LangGraph >=1.2 interrupt-id-keyed resume migration: local in-process
sub-agents must be resumed with an id-keyed map (so >1 pending interrupt does not
raise RuntimeError), while remote A2A sub-agents keep the plain payload (the remote
rebuilds its own resume from the A2A DataPart).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from a2a.types import TaskState
from langgraph.types import Command

from agent_common.a2a.base import LocalA2ARunnable
from agent_common.a2a.client_runnable import A2AClientRunnable
from app.middleware.dynamic_tool_dispatch import _build_subagent_resume_command

# A valid xxh3_128 hexdigest (32 lowercase hex chars) — the format LangGraph uses
# for interrupt ids / namespace hashes.
INTERRUPT_ID = "45fda8478b2ef754419799e10992af06"
DECISIONS = {"decisions": [{"type": "approve"}]}


def _local_runnable() -> LocalA2ARunnable:
    return MagicMock(spec=LocalA2ARunnable)


def _remote_runnable() -> A2AClientRunnable:
    return MagicMock(spec=A2AClientRunnable)


def test_local_runnable_produces_id_keyed_map():
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}]})
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert isinstance(cmd, Command)
    assert cmd.resume == {INTERRUPT_ID: DECISIONS}


def test_remote_runnable_keeps_plain_payload():
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}]})
    cmd = _build_subagent_resume_command(_remote_runnable(), intr, DECISIONS)
    assert cmd.resume == DECISIONS


def test_local_runnable_without_interrupt_id_falls_back_to_plain():
    intr = SimpleNamespace(value={"action_requests": [{"name": "x"}]})  # no .id
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert cmd.resume == DECISIONS


def test_interrupt_id_extracted_from_dict():
    intr = {"id": INTERRUPT_ID, "value": {"action_requests": [{"name": "x"}]}}
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert cmd.resume == {INTERRUPT_ID: DECISIONS}


def test_no_answer_at_all_becomes_empty_payload():
    """Only a missing answer is empty. Words are an answer and must survive.

    They used to be flattened to `{}` here, which is how a typed reply reached the
    sub-agent as "(no reply)" — see the classifier in agent_common.core.hitl_resume.
    """
    intr = SimpleNamespace(id=INTERRUPT_ID, value={})
    assert _build_subagent_resume_command(_local_runnable(), intr, None).resume == {INTERRUPT_ID: {}}
    assert _build_subagent_resume_command(_local_runnable(), intr, "words").resume == {INTERRUPT_ID: "words"}


def test_auth_interrupt_forwards_the_user_reply_verbatim():
    """An auth interrupt resumes with WORDS, and they must survive the trip.

    The HITL-shaped `{}` default used to swallow them: the sub-agent's auth
    middleware then resumed with nothing to act on and fell through with the
    stale auth error, which the sub-agent LLM relayed as prose. The sub-agent is
    the one that has to tell "done, try again" from a refusal, so it needs them.
    """
    intr = SimpleNamespace(
        id=INTERRUPT_ID,
        value={"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"},
    )
    cmd = _build_subagent_resume_command(_local_runnable(), intr, "try again I missclicked")
    assert cmd.resume == {INTERRUPT_ID: "try again I missclicked"}


def test_auth_interrupt_keeps_a_structured_decision():
    """A client that negotiated the extension sends a dict, which passes through."""
    decision = {"authorization": {"decision": "declined", "message": "scopes too wide"}}
    intr = SimpleNamespace(
        id=INTERRUPT_ID,
        value={"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"},
    )
    cmd = _build_subagent_resume_command(_local_runnable(), intr, decision)
    assert cmd.resume == {INTERRUPT_ID: decision}


def test_hitl_interrupt_also_keeps_the_words():
    """Words used to be dropped here; the HITL reader now classifies them.

    A user who types "no, forget it" instead of clicking Reject was answered with
    `{}` — unreadable at the far end — so the call fell through as if nothing had
    been said. See ``agent_common.core.hitl_resume.classify_reply``.
    """
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}]})
    cmd = _build_subagent_resume_command(_local_runnable(), intr, "not-a-dict")
    assert cmd.resume == {INTERRUPT_ID: "not-a-dict"}


def test_none_interrupt_obj_is_safe():
    cmd = _build_subagent_resume_command(_local_runnable(), None, DECISIONS)
    assert cmd.resume == DECISIONS


def test_local_blanket_decision_replicated_to_action_request_count():
    """A single blanket decision is replicated to N for local sub-agents.

    Backstop for non-PTC ConditionalHumanInTheLoopMiddleware sub-agents, which enforce
    one decision per pending call and have no awrap_tool_call replication of their own.
    """
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}, {"name": "y"}]})
    cmd = _build_subagent_resume_command(_local_runnable(), intr, {"decisions": [{"type": "approve"}]})
    assert cmd.resume == {INTERRUPT_ID: {"decisions": [{"type": "approve"}, {"type": "approve"}]}}


def test_local_per_call_decisions_pass_through_unreplicated():
    """A per-call decision list (len != 1) is already aligned — never replicated."""
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}, {"name": "y"}]})
    payload = {"decisions": [{"type": "approve"}, {"type": "reject"}]}
    cmd = _build_subagent_resume_command(_local_runnable(), intr, payload)
    assert cmd.resume == {INTERRUPT_ID: payload}


def test_remote_blanket_decision_not_replicated():
    """Remote sub-agents keep the plain single decision — the remote replicates itself."""
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}, {"name": "y"}]})
    payload = {"decisions": [{"type": "approve"}]}
    cmd = _build_subagent_resume_command(_remote_runnable(), intr, payload)
    assert cmd.resume == payload


# ── The answer and the question can be different questions ──────────────────────
#
# Declining an authorization makes the sub-agent re-run the blocked tool, whose
# risk guard raises a fresh APPROVAL interrupt. The decline — written for the auth
# prompt — was then delivered to that one and reached
# ``interrupt(...)["decisions"]``, killing the sub-agent with KeyError('decisions')
# ("sub-agent execution encountered a system error ('decisions')"), while the skip
# itself was never acknowledged: the card just came back.

AUTH_INTERRUPT = {"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"}
APPROVAL_INTERRUPT = {
    "action_requests": [{"name": "github_get_me", "args": {"_call_id": "github_get_me:4413"}}],
    "review_configs": [{"action_name": "github_get_me", "allowed_decisions": ["approve", "reject"]}],
}


def test_declined_authorization_becomes_an_explicit_rejection():
    """The skip must reject the pending call, not crash the sub-agent."""
    cmd = _build_subagent_resume_command(
        _local_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=APPROVAL_INTERRUPT),
        {"authorization": {"decision": "declined"}},
    )
    decisions = cmd.resume[INTERRUPT_ID]["decisions"]
    assert [d["type"] for d in decisions] == ["reject"]
    assert decisions[0]["id"] == "github_get_me:4413"
    assert "skipped the authorization" in decisions[0]["message"]
    assert "Do not retry" in decisions[0]["message"]


def test_approved_authorization_does_not_approve_the_call():
    """Answering the auth prompt is not approving a call nobody asked about."""
    cmd = _build_subagent_resume_command(
        _local_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=APPROVAL_INTERRUPT),
        {"authorization": {"decision": "approved"}},
    )
    assert [d["type"] for d in cmd.resume[INTERRUPT_ID]["decisions"]] == ["reject"]


def test_rejection_becomes_a_declined_authorization():
    """'Don't run it' and 'don't authorize' are the same no."""
    cmd = _build_subagent_resume_command(
        _local_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=AUTH_INTERRUPT),
        {"decisions": [{"type": "reject", "message": "too wide"}]},
    )
    assert cmd.resume[INTERRUPT_ID] == {"authorization": {"decision": "declined", "message": "too wide"}}


def test_stale_approval_for_an_auth_prompt_is_not_delivered():
    """The replayed approve belongs to a question the sub-agent already moved past.

    Resuming with an EMPTY id-keyed map runs the sub-agent forward without
    answering anything, so its auth interrupt raises again and the orchestrator's
    ``except GraphInterrupt`` handler asks for the answer written for *it*. Feeding
    the stale approve through instead is what produced "(no reply)", the retry, and
    the second approval card.
    """
    cmd = _build_subagent_resume_command(
        _local_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=AUTH_INTERRUPT),
        {"decisions": [{"type": "approve", "id": "github_get_me:4413"}]},
    )
    assert cmd.resume == {}


def test_words_reach_an_approval_prompt_untouched():
    """Free text is classified by the reader, so it must survive the trip."""
    cmd = _build_subagent_resume_command(
        _local_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=APPROVAL_INTERRUPT),
        "no, forget it",
    )
    assert cmd.resume == {INTERRUPT_ID: "no, forget it"}


def test_remote_runnable_still_gets_the_translated_payload():
    cmd = _build_subagent_resume_command(
        _remote_runnable(),
        SimpleNamespace(id=INTERRUPT_ID, value=APPROVAL_INTERRUPT),
        {"authorization": {"decision": "declined"}},
    )
    assert [d["type"] for d in cmd.resume["decisions"]] == ["reject"]


def _held(answer):
    """Patch the orchestrator's own resume probe (its newest authorization answer)."""
    return patch(
        "app.middleware.dynamic_tool_dispatch.pending_authorization_answer",
        return_value=answer,
    )


def test_a_held_decline_beats_the_stale_replay():
    """The refusal must be delivered NOW, not queued behind a second interrupt.

    Holding it back (`Command(resume={})`) assumed the sub-agent would raise its
    auth prompt again. That prompt only exists as a consequence of a ToolException,
    so once the user has completed the login the tool SUCCEEDS, nothing asks again,
    and the refusal is never delivered — the call went through despite the no.
    """
    decline = {"authorization": {"decision": "declined", "message": "no way"}}
    with _held(decline):
        cmd = _build_subagent_resume_command(
            _local_runnable(),
            SimpleNamespace(id=INTERRUPT_ID, value=AUTH_INTERRUPT),
            {"decisions": [{"type": "approve", "id": "github_get_me:4413"}]},  # the stale replay
        )

    assert cmd.resume == {INTERRUPT_ID: decline}


def test_a_held_approval_is_delivered_too():
    approval = {"authorization": {"decision": "approved", "message": "done"}}
    with _held(approval):
        cmd = _build_subagent_resume_command(
            _local_runnable(), SimpleNamespace(id=INTERRUPT_ID, value=AUTH_INTERRUPT), None
        )

    assert cmd.resume == {INTERRUPT_ID: approval}


def test_nothing_held_still_holds_the_stale_approval_back():
    """With no answer to deliver, the old behaviour stands: answer nothing."""
    with _held(None):
        cmd = _build_subagent_resume_command(
            _local_runnable(),
            SimpleNamespace(id=INTERRUPT_ID, value=AUTH_INTERRUPT),
            {"decisions": [{"type": "approve"}]},
        )

    assert cmd.resume == {}


def test_a_held_answer_does_not_touch_a_tool_approval_prompt():
    """It answers the AUTH question; an approval prompt has its own answer."""
    with _held({"authorization": {"decision": "declined"}}):
        cmd = _build_subagent_resume_command(
            _local_runnable(),
            SimpleNamespace(id=INTERRUPT_ID, value=APPROVAL_INTERRUPT),
            {"decisions": [{"type": "approve", "id": "github_get_me:4413"}]},
        )

    assert [d["type"] for d in cmd.resume[INTERRUPT_ID]["decisions"]] == ["approve"]
