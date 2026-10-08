"""Unit tests for ``agent_common.core.hitl_resume``.

The reader every HITL interrupt now goes through. Two questions share the resume
channel (tool approval, authorization) and their answers get crossed, so the rules
under test are: never raise, never invent consent, and never silently swallow an
answer the user actually gave.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import TaskState

from agent_common.core.hitl_resume import (
    HITL_DECISION_EVENT,
    KIND_AUTH,
    KIND_HITL,
    KIND_OTHER,
    _classifier_prompt,
    authorization_from_decisions,
    authorization_verdict,
    decisions_from_resume,
    decisions_from_resume_sync,
    interrupt_kind,
    pending_authorization_answer,
    reject_decisions,
    resume_will_return,
    structural_decisions,
)

ACTION_REQUESTS = [
    {"name": "github_get_me", "args": {"_call_id": "github_get_me:4413", "_summary": "read your profile"}},
]
TWO_ACTION_REQUESTS = ACTION_REQUESTS + [{"name": "send_email", "args": {"_call_id": "send_email:99"}}]


def _classification(intent: str):
    """Patch the fast-LLM classifier to a fixed verdict, for both call styles."""
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=MagicMock(intent=intent))
    model.invoke = MagicMock(return_value=MagicMock(intent=intent))
    return patch("agent_common.core.hitl_resume._classifier_model", return_value=model)


class TestInterruptKind:
    def test_approval(self):
        assert interrupt_kind({"action_requests": []}) == KIND_HITL

    def test_authorization(self):
        assert interrupt_kind({"task_state": TaskState.TASK_STATE_AUTH_REQUIRED}) == KIND_AUTH

    def test_anything_else_is_left_alone(self):
        assert interrupt_kind({"client_action_request": {}}) == KIND_OTHER
        assert interrupt_kind("words") == KIND_OTHER
        assert interrupt_kind(None) == KIND_OTHER


class TestAuthorizationVerdict:
    @pytest.mark.parametrize("word", ["declined", "skipped", "reject", "DECLINED"])
    def test_declines(self, word):
        assert authorization_verdict({"authorization": {"decision": word}})[0] == "declined"

    @pytest.mark.parametrize("word", ["approved", "completed", "done"])
    def test_approves(self, word):
        assert authorization_verdict({"authorization": {"decision": word}})[0] == "approved"

    def test_unknown_verdict_is_not_a_no(self):
        assert authorization_verdict({"authorization": {"decision": "hmm", "message": "later"}}) == (None, "later")

    def test_not_an_authorization_payload(self):
        assert authorization_verdict({"decisions": [{"type": "approve"}]}) == (None, "")


class TestStructuralDecisions:
    def test_real_decisions_pass_through_untouched(self):
        decisions = [{"type": "approve", "id": "github_get_me:4413"}]
        assert structural_decisions({"decisions": decisions}, ACTION_REQUESTS) is decisions

    def test_a_clicked_reject_forbids_the_same_change_another_way(self):
        """A refused server write came back as a form fill plus a save card."""
        decisions = structural_decisions(
            {"decisions": [{"type": "reject", "id": "a"}, {"type": "approve", "id": "b"}]}, TWO_ACTION_REQUESTS
        )
        assert [d["type"] for d in decisions] == ["reject", "approve"]
        message = decisions[0]["message"]
        assert "NOT executed" in message
        assert "do not make the same change another way" in message
        assert decisions[0]["id"] == "a" and "message" not in decisions[1]

    def test_a_reason_given_with_reject_is_kept_and_framed(self):
        decisions = structural_decisions(
            {"decisions": [{"type": "reject", "message": "keep job 1, it is a fixture"}]}, ACTION_REQUESTS
        )
        message = decisions[0]["message"]
        assert "Their reason: keep job 1, it is a fixture" in message
        assert message.startswith("The user clicked Reject")

    def test_declined_authorization_rejects_every_pending_call(self):
        decisions = structural_decisions(
            {"authorization": {"decision": "declined", "message": "not now"}}, TWO_ACTION_REQUESTS
        )
        assert [d["type"] for d in decisions] == ["reject", "reject"]
        assert [d["id"] for d in decisions] == ["github_get_me:4413", "send_email:99"]
        assert "not now" in decisions[0]["message"]

    def test_approved_authorization_is_not_consent_to_the_call(self):
        decisions = structural_decisions({"authorization": {"decision": "approved"}}, ACTION_REQUESTS)
        assert [d["type"] for d in decisions] == ["reject"]
        assert "has not approved this call" in decisions[0]["message"]

    def test_empty_payload_rejects_rather_than_raising(self):
        # This is the KeyError('decisions') crash site, from the other direction.
        assert [d["type"] for d in structural_decisions({}, ACTION_REQUESTS)] == ["reject"]

    def test_words_are_left_for_the_classifier(self):
        assert structural_decisions("no, forget it", ACTION_REQUESTS) is None


class TestDecisionsFromResume:
    @pytest.mark.asyncio
    async def test_words_meaning_yes_approve(self):
        with _classification("approve"):
            decisions = await decisions_from_resume("yes, go ahead", ACTION_REQUESTS)
        assert decisions == [{"type": "approve", "id": "github_get_me:4413"}]

    @pytest.mark.asyncio
    async def test_words_meaning_no_reject_with_the_reason(self):
        with _classification("reject"):
            decisions = await decisions_from_resume("no, those permissions are too wide", ACTION_REQUESTS)
        assert decisions[0]["type"] == "reject"
        assert "too wide" in decisions[0]["message"]

    @pytest.mark.asyncio
    async def test_a_refusal_tells_the_model_to_stop(self):
        with _classification("reject"):
            message = (await decisions_from_resume("no", ACTION_REQUESTS))[0]["message"]
        assert "REFUSED" in message
        assert "Do not retry it" in message

    @pytest.mark.asyncio
    async def test_unrelated_words_never_approve_but_stay_askable(self):
        """An off-topic message is not a refusal, and must not dead-end the task.

        Treating it as one consumed the approval and forbade asking again, so a
        question typed while the card was open silently killed the work.
        """
        with _classification("unclear"):
            decisions = await decisions_from_resume("what's the weather?", ACTION_REQUESTS)

        assert [d["type"] for d in decisions] == ["reject"]
        message = decisions[0]["message"]
        assert "not a refusal" in message
        assert "ask for the call again" in message
        assert "Do not retry" not in message

    @pytest.mark.asyncio
    async def test_a_question_is_answered_not_treated_as_a_no(self):
        """"what\'s this?" while the card is open is a question, not a refusal.

        Bucketed as a refusal, the agent replied "the user rejected the call, as
        instructed it was not retried" — and never answered the question. The
        classifier gets its own intent for it, and the message orders an answer
        followed by a fresh ask.
        """
        with _classification("question"):
            decisions = await decisions_from_resume("what's this?", ACTION_REQUESTS)

        assert [d["type"] for d in decisions] == ["reject"]
        message = decisions[0]["message"]
        assert "NOT a refusal" in message
        assert "ANSWER THEIR QUESTION first" in message
        assert "ask them to approve it again" in message
        assert "what's this?" in message

    @pytest.mark.asyncio
    async def test_no_hitl_refusal_lets_the_agent_deny_the_tool_exists(self):
        """The orchestrator answered "I don\'t have a GitHub tool in this environment".

        A blocked call is not a missing one, and every non-approval says so — with
        the right cause: not approved, which is not the same as not authorized.
        """
        for intent in ("reject", "question", "unclear"):
            with _classification(intent):
                message = (await decisions_from_resume("hm", ACTION_REQUESTS))[0]["message"]
            assert "NOT missing or unavailable" in message
            assert "was not approved to run" in message
            assert "unavailable in this environment" in message
            assert "no authorization" not in message  # that is the auth path's cause

    @pytest.mark.asyncio
    async def test_a_broken_classifier_never_approves(self):
        """No signal is not consent — but it is not a refusal either."""
        with patch("agent_common.core.hitl_resume._classifier_model", side_effect=RuntimeError("no model")):
            decisions = await decisions_from_resume("yes please", ACTION_REQUESTS)
        assert [d["type"] for d in decisions] == ["reject"]
        assert "ask for the call again" in decisions[0]["message"]

    @pytest.mark.asyncio
    async def test_always_one_decision_per_pending_call(self):
        """The count check downstream raises ValueError on a mismatch."""
        for resume in ({}, None, {"authorization": {"decision": "declined"}}, "gibberish"):
            with _classification("unclear"):
                assert len(await decisions_from_resume(resume, TWO_ACTION_REQUESTS)) == 2

    @pytest.mark.asyncio
    async def test_a_requested_change_is_not_approval(self):
        """'pick the premium chat tier' while a save waited: a change, not consent."""
        with _classification("change"):
            decisions = await decisions_from_resume("pick the premium chat tier", ACTION_REQUESTS)
        assert [d["type"] for d in decisions] == ["reject"]
        message = decisions[0]["message"]
        assert "asked to CHANGE this call" in message
        assert "pick the premium chat tier" in message

    @pytest.mark.asyncio
    async def test_typed_words_never_approve_a_save(self):
        """A form's save writes it to the server: only the click counts, never typed words."""
        save = [
            {
                "name": "client_action",
                "args": {"kind": "invoke", "target_type": "SubAgent", "target_id": "new", "action": "save", "_requires_click": True},
            }
        ]
        with _classification("approve"):
            decisions = await decisions_from_resume("yes", save)
        assert [d["type"] for d in decisions] == ["reject"]
        message = decisions[0]["message"]
        assert "it needs the user to click Approve" in message
        # "yes save it" must not read as "confirm the save": the agent claimed it was
        # saved. The answered card is closed, so a click needs a fresh invoke.
        assert "Do NOT tell them it was saved" in message
        assert "send the same invoke again" in message
        # A clicked approval (structural) still runs it.
        assert (await decisions_from_resume({"decisions": [{"type": "approve"}]}, save))[0]["type"] == "approve"

    @pytest.mark.asyncio
    async def test_typed_words_never_run_an_action_that_requires_approval(self):
        """A host-marked action saves too: like a save, only the click runs it."""
        marked = [
            {
                "name": "client_action",
                "args": {"kind": "invoke", "action": "set_tier_default", "_requires_click": True},
            }
        ]
        with _classification("approve"):
            decisions = await decisions_from_resume("yes do it", marked)
        assert [d["type"] for d in decisions] == ["reject"]
        assert "It was NOT done" in decisions[0]["message"]
        assert "send the same invoke again" in decisions[0]["message"]

    def test_sync_twin_classifies_too(self):
        with _classification("reject"):
            decisions = decisions_from_resume_sync("stop", ACTION_REQUESTS)
        assert [d["type"] for d in decisions] == ["reject"]

    @pytest.mark.asyncio
    async def test_only_the_users_words_are_classified(self):
        """Slack and Google Chat put the thread's history before the new message.

        The classifier read the bot's own "Approval required" lines along with the
        user's "maybe later", and called it an approval: the call ran.
        """
        wrapped = (
            "<thread_context>\n"
            '<message role="assistant" userName="Nannos">Approval required</message>\n'
            "</thread_context>\n"
            '<current_request userId="U1" userName="Andrea">maybe later</current_request>'
        )
        with _classification("unclear") as model_factory:
            decisions = await decisions_from_resume(wrapped, ACTION_REQUESTS)
            sync_decisions = decisions_from_resume_sync(wrapped, ACTION_REQUESTS)

        prompt = model_factory.return_value.ainvoke.call_args.args[0][1]["content"]
        assert prompt.endswith("The user replied:\nmaybe later")
        assert "Approval required" not in prompt
        assert model_factory.return_value.invoke.call_args.args[0][1]["content"] == prompt
        for d in (decisions[0], sync_decisions[0]):
            assert "They said: maybe later " in d["message"]
            assert "thread_context" not in d["message"]

    def test_a_deferral_is_not_approval(self):
        """The rule the fast model needs for "maybe later" / "not yet"."""
        system = _classifier_prompt("maybe later", ACTION_REQUESTS, None)[0]["content"]
        assert "'maybe later'" in system
        assert "NOT approval" in system


class TestTypedDecisionAnnouncement:
    """Words are read server-side, so the client is told what they did to its card."""

    @staticmethod
    def _writer():
        events: list = []
        return events, patch("langgraph.config.get_stream_writer", return_value=events.append)

    @pytest.mark.asyncio
    async def test_classified_words_announce_id_type_and_intent(self):
        events, writer = self._writer()
        with writer, _classification("change"):
            await decisions_from_resume("use the premium tier", ACTION_REQUESTS)
        assert events == [
            (HITL_DECISION_EVENT, {"decisions": [{"id": "github_get_me:4413", "type": "reject", "intent": "change"}]})
        ]

    @pytest.mark.asyncio
    async def test_a_typed_yes_to_a_save_reads_approve_but_did_not_run(self):
        save = [{"name": "client_action", "args": {"_call_id": "c1", "kind": "invoke", "_requires_click": True}}]
        events, writer = self._writer()
        with writer, _classification("approve"):
            await decisions_from_resume("yes", save)
        assert events[0][1]["decisions"] == [{"id": "c1", "type": "reject", "intent": "approve"}]

    @pytest.mark.asyncio
    async def test_an_unclear_reply_reads_as_none(self):
        events, writer = self._writer()
        with writer, _classification("unclear"):
            await decisions_from_resume("what's the weather?", ACTION_REQUESTS)
        assert events[0][1]["decisions"][0]["intent"] == "none"

    @pytest.mark.asyncio
    async def test_a_click_announces_nothing(self):
        events, writer = self._writer()
        with writer:
            await decisions_from_resume({"decisions": [{"type": "approve", "id": "x"}]}, ACTION_REQUESTS)
        assert events == []

    def test_sync_twin_announces_too(self):
        events, writer = self._writer()
        with writer, _classification("reject"):
            decisions_from_resume_sync("stop", ACTION_REQUESTS)
        assert events[0][1]["decisions"][0]["intent"] == "reject"

    @pytest.mark.asyncio
    async def test_no_stream_writer_is_not_an_error(self):
        # Outside a runnable context get_stream_writer raises: nobody to tell.
        with _classification("approve"):
            decisions = await decisions_from_resume("yes", ACTION_REQUESTS)
        assert decisions[0]["type"] == "approve"


class TestAuthorizationFromDecisions:
    def test_rejection_carries_over_as_a_decline(self):
        assert authorization_from_decisions([{"type": "reject", "message": "too wide"}]) == {
            "decision": "declined",
            "message": "too wide",
        }

    def test_approval_does_not_carry_over(self):
        """The user approved a call; nobody asked them about the authorization."""
        assert authorization_from_decisions([{"type": "approve"}]) is None

    def test_mixed_batch_does_not_carry_over(self):
        assert authorization_from_decisions([{"type": "reject"}, {"type": "approve"}]) is None

    def test_nothing_to_read(self):
        assert authorization_from_decisions([]) is None
        assert authorization_from_decisions(None) is None


def test_reject_decisions_carry_the_reason_the_model_reads():
    decisions = reject_decisions(ACTION_REQUESTS, "because reasons")
    assert decisions == [{"type": "reject", "message": "because reasons", "id": "github_get_me:4413"}]


def test_skipped_authorization_rejection_stays_inside_this_environment():
    """The rejection the model reads must not invite out-of-product remedies."""
    message = structural_decisions({"authorization": {"decision": "declined"}}, ACTION_REQUESTS)[0]["message"]
    assert "NOT missing or unavailable" in message
    assert "personal access tokens" in message
    assert "authorization prompt in this conversation" in message


# ── Reading LangGraph's resume state ────────────────────────────────────────────
#
# `scratchpad.resume` is a REPLAY LOG, not a queue of unanswered questions. Testing
# it for emptiness says "this task has answered an interrupt before" — true from
# the second interrupt onwards, whether or not the next one is about to raise.


class _Scratchpad:
    """Stands in for LangGraph's PregelScratchpad, counter semantics included."""

    def __init__(self, resume, interrupts_taken=0, null_resume=None):
        import itertools

        self.resume = list(resume)
        self._null_resume = null_resume
        count = itertools.count(0)
        for _ in range(interrupts_taken):
            next(count)

        class _Counter:
            _counter = count.__next__

        self.interrupt_counter = _Counter()

    def get_null_resume(self, consume=False):
        assert consume is False, "the probe must never consume the real interrupt's value"
        return self._null_resume


def _with_scratchpad(scratchpad):
    return patch("agent_common.core.hitl_resume._scratchpad", return_value=scratchpad)


class TestResumeWillReturn:
    def test_first_interrupt_of_a_replay_returns(self):
        with _with_scratchpad(_Scratchpad(["approve it"], interrupts_taken=0)):
            assert resume_will_return() is True

    def test_second_card_of_a_multi_round_eval_is_about_to_raise(self):
        """The regression: round 2 raises a NEW card, and it needs its summaries.

        One answered interrupt makes `resume` non-empty forever, so a truthiness
        check called every later card a replay and skipped their summaries — the
        cards whose raw args are least readable.
        """
        with _with_scratchpad(_Scratchpad(["approve it"], interrupts_taken=1)):
            assert resume_will_return() is False

    def test_a_freshly_queued_answer_counts(self):
        with _with_scratchpad(_Scratchpad(["approve it"], interrupts_taken=1, null_resume="go on")):
            assert resume_will_return() is True

    def test_a_consumed_null_resume_does_not_count_again(self):
        """LangGraph's non-consuming read still returns a null resume an interrupt has taken."""
        answer = "go on"
        with _with_scratchpad(_Scratchpad([answer], interrupts_taken=1, null_resume=answer)):
            assert resume_will_return() is False

    def test_nothing_answered_yet(self):
        with _with_scratchpad(_Scratchpad([], interrupts_taken=0)):
            assert resume_will_return() is False

    def test_a_broken_probe_summarizes_as_usual(self):
        with patch("agent_common.core.hitl_resume._scratchpad", side_effect=RuntimeError("no config")):
            assert resume_will_return() is False


class TestPendingAuthorizationAnswer:
    def test_found_behind_a_settled_tool_approval(self):
        """The auth answer is rarely the value the next interrupt consumes."""
        answer = {"authorization": {"decision": "declined"}}
        with _with_scratchpad(_Scratchpad([{"decisions": [{"type": "approve"}]}, answer])):
            assert pending_authorization_answer() == answer

    def test_newest_answer_wins(self):
        old = {"authorization": {"decision": "declined"}}
        new = {"authorization": {"decision": "approved"}}
        with _with_scratchpad(_Scratchpad([old, new])):
            assert pending_authorization_answer() == new

    def test_a_queued_answer_is_seen_before_any_interrupt_consumes_it(self):
        answer = {"authorization": {"decision": "declined"}}
        with _with_scratchpad(_Scratchpad([], null_resume=answer)):
            assert pending_authorization_answer() == answer

    def test_nothing_authorization_shaped(self):
        with _with_scratchpad(_Scratchpad([{"decisions": [{"type": "approve"}]}, "words"])):
            assert pending_authorization_answer() is None


def test_the_counter_probe_matches_the_real_langgraph_scratchpad():
    """Pins the probe against LangGraph's own object, not just our stand-in.

    The index is read off `itertools.count`'s repr because CPython 3.14 dropped
    both copy and pickle for it. If LangGraph ever swaps the counter type, this
    fails here rather than silently reporting index 0 forever — which would make
    every card look like a replay again.
    """
    from langgraph.pregel._algo import _scratchpad as build_scratchpad

    from agent_common.core.hitl_resume import _next_interrupt_index

    scratchpad = build_scratchpad(
        parent_scratchpad=None,
        pending_writes=[],
        task_id="task-1",
        namespace_hash="ns-1",
        resume_map=None,
        step=1,
        stop=10,
    )

    assert _next_interrupt_index(scratchpad) == 0
    scratchpad.interrupt_counter()
    scratchpad.interrupt_counter()
    assert _next_interrupt_index(scratchpad) == 2


class TestRefusalsAreReportedAsTheUsers:
    """Reported in the passive, a refusal was blamed on the service: "Gmail rejected it"."""

    @pytest.mark.asyncio
    async def test_a_clicked_reject_says_the_user_declined_it(self):
        decisions = await decisions_from_resume({"decisions": [{"type": "reject"}]}, ACTION_REQUESTS)
        message = decisions[0]["message"]
        assert "the user declined it at the approval prompt" in message
        assert "never that the service, the tool or an authorization rejected it" in message

    @pytest.mark.asyncio
    async def test_a_typed_refusal_says_so_too(self):
        with _classification("reject"):
            decisions = await decisions_from_resume("no, don't", ACTION_REQUESTS)
        assert "the user declined it at the approval prompt" in decisions[0]["message"]

    @pytest.mark.asyncio
    async def test_a_non_answer_is_not_reported_as_a_refusal(self):
        with _classification("unclear"):
            decisions = await decisions_from_resume("what's the weather?", ACTION_REQUESTS)
        assert "declined it" not in decisions[0]["message"]


class TestTheAuthorizationReaderGetsOnlyTheWordsToo:
    @pytest.mark.asyncio
    async def test_classify_reply_strips_the_thread_for_every_caller(self):
        """The sign-in classifier read the bot's own "Authorize in the browser…" lines."""
        from agent_common.core.hitl_resume import classify_reply

        wrapped = (
            "<thread_context>\n<message role=\"assistant\">Authorize in the browser, then confirm here</message>\n"
            "</thread_context>\n<current_request userId=\"U1\">maybe later</current_request>"
        )
        with _classification("unclear") as model_factory:
            await classify_reply(wrapped, [], question="authorize?")
        prompt = model_factory.return_value.ainvoke.call_args.args[0][1]["content"]
        assert prompt.endswith("maybe later")
        assert "Authorize in the browser" not in prompt
