"""Conversation titling: parsing, the first-exchange pick, the run-once guard, and
the page-context stamp that tells the list where a conversation started."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from console_backend.services import conversation_summary as cs
from console_backend.services.conversation_service import conversation_page_context


#: The answer a turn just produced — what the caller hands the titler.
ANSWER = "Its daily cap was lowered on Monday, so it stopped spending."


# ---- parsing ---------------------------------------------------------------


def test_parses_plain_json():
    parsed = cs.parse_summary_response('{"title": "Campaign 42 pacing", "summary": "Why campaign 42 under-delivered."}')
    assert parsed == ("Campaign 42 pacing", "Why campaign 42 under-delivered.")


def test_parses_through_fences_and_chatter():
    raw = 'Sure!\n```json\n{"title": "Invoice mismatch", "summary": "The August invoice does not match the booking."}\n```'
    assert cs.parse_summary_response(raw) == (
        "Invoice mismatch",
        "The August invoice does not match the booking.",
    )


def test_normalizes_a_messy_title():
    parsed = cs.parse_summary_response('{"title": "  \\"Campaign\\n 42  pacing.\\"  ", "summary": "  A  sentence.  "}')
    assert parsed == ("Campaign 42 pacing", "A sentence.")


def test_caps_what_it_stores():
    long_title = "word " * 40
    parsed = cs.parse_summary_response(f'{{"title": "{long_title}", "summary": "{long_title}"}}')
    assert parsed is not None
    title, summary = parsed
    assert len(title) <= cs.MAX_TITLE_CHARS
    assert len(summary) <= cs.MAX_SUMMARY_CHARS


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "I could not summarize that.",
        '{"title": "Only a title"}',
        '{"title": "", "summary": "empty title"}',
        '{"title": 42, "summary": "wrong type"}',
        '{"broken": ',
        "[1, 2, 3]",
    ],
)
def test_rejects_anything_incomplete(raw):
    # Half an answer must never overwrite an existing title.
    assert cs.parse_summary_response(raw) is None


# ---- what counts as an answer ---------------------------------------------


def test_takes_a_real_answer_and_normalizes_whitespace():
    assert cs.usable_answer("  It is  12%\n behind.  ") == "It is 12% behind."


def test_an_answer_that_opens_with_the_word_status_is_still_an_answer():
    text = "Status: the campaign is fine — pacing recovered on Tuesday."
    assert cs.usable_answer(text) == text


def test_truncates_a_long_answer():
    assert len(cs.usable_answer("a" * 9000)) == cs.MAX_INPUT_CHARS


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        # Placeholder rows the persistence layer writes for a bare status event.
        # Titling from one would burn the single attempt we get.
        "Status: completed at 2026-08-26T12:00:00Z",
        "status: working",
        "Task completed: t-1",
    ],
)
def test_refuses_a_non_answer(text):
    assert cs.usable_answer(text) == ""


# ---- the run-once guard and the happy path --------------------------------


def fake_services(metadata=None, title="Why is campaign 42 und"):
    """A conversation whose title is its opening user message — the question."""
    conversation = SimpleNamespace(metadata=metadata or {}, title=title)
    return SimpleNamespace(
        get_conversation=AsyncMock(return_value=conversation),
        update_summary=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_titles_a_conversation_and_stores_both_fields(monkeypatch):
    conversation_service = fake_services()
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    gateway = AsyncMock(return_value='{"title": "Campaign 42 pacing", "summary": "Why campaign 42 under-delivered."}')
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer=ANSWER, user_sub="user-1"
    )
    conversation_service.update_summary.assert_awaited_once_with(
        "conv-1",
        "user-1",
        title="Campaign 42 pacing",
        summary="Why campaign 42 under-delivered.",
        title_source="llm",
    )
    # Both halves reached the model — the title as the question, the answer the
    # caller passed — and cost is attributed to the OIDC sub.
    prompt = gateway.await_args.args[0]
    assert "Why is campaign 42 und" in prompt
    assert "daily cap was lowered" in prompt
    assert gateway.await_args.kwargs["metadata"] == {"user_sub": "user-1", "conversation_id": "conv-1"}


@pytest.mark.asyncio
async def test_asks_for_no_thinking_but_keeps_the_token_headroom(monkeypatch):
    """Titling pays for thinking tokens on every new conversation and gains nothing
    from them, so it asks for thinking off. The high max_tokens stays as the fallback
    for a model that ignores that: an unused ceiling costs nothing, a truncated one
    costs the title."""
    conversation_service = fake_services()
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    gateway = AsyncMock(return_value='{"title": "T", "summary": "S"}')
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)
    assert gateway.await_args.kwargs["reasoning_effort"] == "none"
    assert gateway.await_args.kwargs["max_tokens"] >= 4000


@pytest.mark.asyncio
async def test_notifies_viewers_once_the_name_is_stored(monkeypatch):
    conversation_service = fake_services()
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    monkeypatch.setattr(
        cs,
        "gateway_chat",
        AsyncMock(return_value='{"title": "Campaign 42 pacing", "summary": "Why it under-delivered."}'),
    )
    notified = AsyncMock()

    assert await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer=ANSWER, on_stored=notified
    )
    notified.assert_awaited_once_with("Campaign 42 pacing", "Why it under-delivered.")


@pytest.mark.asyncio
async def test_no_notification_when_nothing_was_stored(monkeypatch):
    conversation_service = fake_services()
    conversation_service.update_summary = AsyncMock(return_value=False)  # row vanished
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    monkeypatch.setattr(
        cs, "gateway_chat", AsyncMock(return_value='{"title": "T", "summary": "S"}')
    )
    notified = AsyncMock()

    assert not await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer=ANSWER, on_stored=notified
    )
    notified.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_placeholder_answer_is_not_worth_a_title(monkeypatch):
    """The regression this guards: a bare status row used to look like an answer,
    burning the one attempt on a title written from "Status: completed"."""
    conversation_service = fake_services()
    gateway = AsyncMock()
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert not await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer="Status: completed at 2026-08-26T12:00:00Z"
    )
    gateway.assert_not_awaited()
    conversation_service.update_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_untitled_conversation_has_no_question_to_work_from(monkeypatch):
    """No opening message was stored (a conversation that began at an approval),
    so there is nothing to name it after yet."""
    conversation_service = fake_services(title="")
    gateway = AsyncMock()
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert not await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer=ANSWER
    )
    gateway.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_only_once_per_conversation(monkeypatch):
    conversation_service = fake_services(metadata={"title_source": "llm"})
    gateway = AsyncMock()
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert not await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)
    gateway.assert_not_awaited()
    conversation_service.update_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_model_configured_keeps_the_existing_title(monkeypatch):
    conversation_service = fake_services()
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value=None))
    gateway = AsyncMock()
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert not await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)
    gateway.assert_not_awaited()
    conversation_service.update_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gateway_failure_is_swallowed(monkeypatch):
    conversation_service = fake_services()
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    monkeypatch.setattr(cs, "gateway_chat", AsyncMock(side_effect=RuntimeError("gateway down")))

    # Detached task: it must never raise, and never touch the title.
    assert not await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)
    conversation_service.update_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_read_failure_is_swallowed(monkeypatch):
    conversation_service = fake_services()
    conversation_service.get_conversation = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))

    assert not await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)


@pytest.mark.asyncio
async def test_unknown_conversation_is_a_no_op():
    conversation_service = fake_services()
    conversation_service.get_conversation = AsyncMock(return_value=None)
    assert not await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)


# ---- the page-context stamp ----------------------------------------------


def test_keeps_the_stable_slice_of_the_page_context():
    origin = conversation_page_context(
        {
            "key": "/campaigns/123",
            "title": "Campaign 42",
            "entity": {"type": "Campaign", "id": "123", "name": "Summer sale"},
            # Volatile: belongs to the send's prompt, not to the conversation.
            "view": {"tab": "delivery"},
            "visible": ["row 1", "row 2"],
            "breadcrumbs": ["Campaigns", "Campaign 42"],
        }
    )
    assert origin == {
        "key": "/campaigns/123",
        "title": "Campaign 42",
        "entity": {"type": "Campaign", "id": "123", "name": "Summer sale"},
    }


def test_page_context_caps_and_coerces():
    origin = conversation_page_context({"key": "k" * 900, "title": "t" * 400, "entity": {"type": "Campaign", "id": 123}})
    assert len(origin["key"]) == 500
    assert len(origin["title"]) == 160
    # A numeric id crosses the wire as a number; it is stored as text.
    assert origin["entity"] == {"type": "Campaign", "id": "123"}


@pytest.mark.parametrize(
    "payload",
    [None, "not-a-dict", {}, {"view": {"tab": "x"}}, {"key": "   "}],
)
def test_page_context_stores_nothing_useless(payload):
    assert conversation_page_context(payload) is None


def test_half_an_entity_is_dropped():
    # A type with no id names nothing the list could show.
    origin = conversation_page_context({"key": "/x", "entity": {"type": "Campaign"}})
    assert origin == {"key": "/x"}


@pytest.mark.asyncio
async def test_a_name_the_user_typed_is_never_overwritten(monkeypatch):
    """Renaming happens before the titler runs, not after it. The summary is
    still worth writing — the title is not."""
    conversation_service = fake_services(metadata={"title_source": "user"})
    monkeypatch.setattr(cs, "resolve_summary_model", AsyncMock(return_value="chat-low"))
    monkeypatch.setattr(
        cs,
        "gateway_chat",
        AsyncMock(return_value='{"title": "Campaign 42 pacing", "summary": "Why it under-delivered."}'),
    )

    assert await cs.maybe_summarize_conversation(conversation_service, "conv-1", "user-1", answer=ANSWER)
    conversation_service.update_summary.assert_awaited_once_with(
        "conv-1",
        "user-1",
        title="Why is campaign 42 und",  # the conversation's existing name, untouched
        summary="Why it under-delivered.",
        # Written back, so a later turn cannot overwrite the name either.
        title_source="user",
    )


@pytest.mark.asyncio
async def test_a_user_named_conversation_that_already_has_a_summary_is_done(monkeypatch):
    """Nothing left to write, so it must not pay the model on every turn."""
    conversation_service = fake_services(
        metadata={"title_source": "user", "summary": "Already written."}
    )
    gateway = AsyncMock()
    monkeypatch.setattr(cs, "gateway_chat", gateway)

    assert not await cs.maybe_summarize_conversation(
        conversation_service, "conv-1", "user-1", answer=ANSWER
    )
    gateway.assert_not_awaited()
    conversation_service.update_summary.assert_not_awaited()
