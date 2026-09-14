"""Unit tests for ``local_server.resume.build_resume_command``.

The in-process server delivers a client's answer to the interrupts a sub-agent
graph is parked on. Two things have to hold: the map is keyed by interrupt id
(LangGraph >=1.2 raises on a bare ``Command(resume=value)`` whenever more than
one interrupt is pending), and the answer is fitted to the question actually
asked — an authorization answer arriving at an approval prompt, or vice versa,
must be translated or held back, never delivered raw.

These used to live in the orchestrator (``_build_subagent_resume_command``),
which built the resume on the sub-agent's behalf; now the sub-agent's own server
does it, for any caller.
"""

from types import SimpleNamespace

from a2a.types import TaskState
from langgraph.types import Command, Interrupt

from agent_common.a2a.local_server.resume import (
    ANSWER_NOTHING,
    align_answer_to_interrupt,
    build_resume_command,
    replicate_blanket_decision,
)

# A valid xxh3_128 hexdigest (32 lowercase hex chars) — the format LangGraph uses
# for interrupt ids / namespace hashes.
INTERRUPT_ID = "45fda8478b2ef754419799e10992af06"
OTHER_ID = "0f3a6c3c8d2e4b5a9c1d7e6f5a4b3c2d"
DECISIONS = {"decisions": [{"type": "approve"}]}


def _intr(value, intr_id=INTERRUPT_ID):
    return Interrupt(value=value, id=intr_id)


def test_produces_id_keyed_map():
    cmd = build_resume_command([_intr({"action_requests": [{"name": "x"}]})], DECISIONS)
    assert isinstance(cmd, Command)
    assert cmd.resume == {INTERRUPT_ID: DECISIONS}


def test_interrupt_without_id_falls_back_to_plain():
    intr = SimpleNamespace(value={"action_requests": [{"name": "x"}]})  # no .id
    assert build_resume_command([intr], DECISIONS).resume == DECISIONS


def test_interrupt_id_extracted_from_dict():
    intr = {"id": INTERRUPT_ID, "value": {"action_requests": [{"name": "x"}]}}
    assert build_resume_command([intr], DECISIONS).resume == {INTERRUPT_ID: DECISIONS}


def test_no_answer_at_all_becomes_empty_payload():
    """Only a missing answer is empty. Words are an answer and must survive.

    They used to be flattened to `{}`, which is how a typed reply reached the
    sub-agent as "(no reply)" — see the classifier in agent_common.core.hitl_resume.
    """
    assert build_resume_command([_intr({})], None).resume == {INTERRUPT_ID: {}}
    assert build_resume_command([_intr({})], "words").resume == {INTERRUPT_ID: "words"}


def test_auth_interrupt_forwards_the_user_reply_verbatim():
    """An auth interrupt resumes with WORDS, and they must survive the trip.

    The sub-agent's auth middleware is the one that has to tell "done, try again"
    from a refusal, so it needs them.
    """
    intr = _intr({"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"})
    assert build_resume_command([intr], "try again I missclicked").resume == {INTERRUPT_ID: "try again I missclicked"}


def test_auth_interrupt_keeps_a_structured_decision():
    decision = {"authorization": {"decision": "declined", "message": "scopes too wide"}}
    intr = _intr({"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"})
    assert build_resume_command([intr], decision).resume == {INTERRUPT_ID: decision}


def test_hitl_interrupt_also_keeps_the_words():
    intr = _intr({"action_requests": [{"name": "x"}]})
    assert build_resume_command([intr], "not-a-dict").resume == {INTERRUPT_ID: "not-a-dict"}


def test_no_pending_interrupts_hands_the_answer_through():
    assert build_resume_command([], DECISIONS).resume == DECISIONS


def test_blanket_decision_replicated_to_action_request_count():
    """Non-PTC ConditionalHumanInTheLoopMiddleware sub-agents enforce one decision per call."""
    intr = _intr({"action_requests": [{"name": "x"}, {"name": "y"}]})
    cmd = build_resume_command([intr], {"decisions": [{"type": "approve"}]})
    assert cmd.resume == {INTERRUPT_ID: {"decisions": [{"type": "approve"}, {"type": "approve"}]}}


def test_per_call_decisions_pass_through_unreplicated():
    intr = _intr({"action_requests": [{"name": "x"}, {"name": "y"}]})
    payload = {"decisions": [{"type": "approve"}, {"type": "reject"}]}
    assert build_resume_command([intr], payload).resume == {INTERRUPT_ID: payload}


def test_replicate_helper_leaves_non_dicts_alone():
    assert replicate_blanket_decision("words", {"action_requests": [{}, {}]}) == "words"
    assert replicate_blanket_decision({"decisions": [{"type": "approve"}]}, "not-a-dict") == {
        "decisions": [{"type": "approve"}]
    }


def test_every_pending_interrupt_gets_an_entry():
    """Two parallel tool nodes, two interrupts, one answer each — keyed by id."""
    cmd = build_resume_command(
        [_intr({"action_requests": [{"name": "x"}]}), _intr({"action_requests": [{"name": "y"}]}, OTHER_ID)],
        DECISIONS,
    )
    assert set(cmd.resume) == {INTERRUPT_ID, OTHER_ID}


# ── The answer and the question can be different questions ──────────────────────
#
# Declining an authorization makes the sub-agent re-run the blocked tool, whose
# risk guard raises a fresh APPROVAL interrupt. The decline — written for the auth
# prompt — was then delivered to that one and reached
# ``interrupt(...)["decisions"]``, killing the sub-agent with KeyError('decisions').

AUTH_INTERRUPT = {"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "eval"}
APPROVAL_INTERRUPT = {
    "action_requests": [{"name": "github_get_me", "args": {"_call_id": "github_get_me:4413"}}],
    "review_configs": [{"action_name": "github_get_me", "allowed_decisions": ["approve", "reject"]}],
}


def test_declined_authorization_becomes_an_explicit_rejection():
    cmd = build_resume_command([_intr(APPROVAL_INTERRUPT)], {"authorization": {"decision": "declined"}})
    decisions = cmd.resume[INTERRUPT_ID]["decisions"]
    assert [d["type"] for d in decisions] == ["reject"]
    assert decisions[0]["id"] == "github_get_me:4413"
    assert "skipped the authorization" in decisions[0]["message"]
    assert "Do not retry" in decisions[0]["message"]


def test_approved_authorization_does_not_approve_the_call():
    cmd = build_resume_command([_intr(APPROVAL_INTERRUPT)], {"authorization": {"decision": "approved"}})
    assert [d["type"] for d in cmd.resume[INTERRUPT_ID]["decisions"]] == ["reject"]


def test_rejection_becomes_a_declined_authorization():
    cmd = build_resume_command([_intr(AUTH_INTERRUPT)], {"decisions": [{"type": "reject", "message": "too wide"}]})
    assert cmd.resume[INTERRUPT_ID] == {"authorization": {"decision": "declined", "message": "too wide"}}


def test_stale_approval_for_an_auth_prompt_is_not_delivered():
    """Resuming with an EMPTY map runs the graph forward; the auth prompt raises again."""
    cmd = build_resume_command([_intr(AUTH_INTERRUPT)], {"decisions": [{"type": "approve", "id": "github_get_me:4413"}]})
    assert cmd.resume == {}
    assert align_answer_to_interrupt({"decisions": [{"type": "approve"}]}, AUTH_INTERRUPT) is ANSWER_NOTHING


def test_words_reach_an_approval_prompt_untouched():
    assert build_resume_command([_intr(APPROVAL_INTERRUPT)], "no, forget it").resume == {INTERRUPT_ID: "no, forget it"}


# ── Client-action round trips ───────────────────────────────────────────────────

CLIENT_ACTION_INTERRUPT = {"client_action_request": {"id": "call-9", "directive": {"kind": "apply"}}}


def test_client_action_result_is_matched_by_request_id():
    answer = {"decisions": [{"id": "call-9", "type": "approve", "client_action_result": {"ok": True}}]}
    assert build_resume_command([_intr(CLIENT_ACTION_INTERRUPT)], answer).resume == {INTERRUPT_ID: {"ok": True}}


def test_client_action_without_a_result_is_an_explicit_no_result():
    cmd = build_resume_command([_intr(CLIENT_ACTION_INTERRUPT)], "the user typed something else")
    assert cmd.resume == {INTERRUPT_ID: {"ok": False, "reason": "no-result"}}
