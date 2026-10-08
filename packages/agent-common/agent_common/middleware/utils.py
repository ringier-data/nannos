"""Utility functions for middleware."""

from langchain_core.messages import AIMessage, AnyMessage, ContentBlock, HumanMessage, SystemMessage


def append_to_system_message(
    system_message: SystemMessage | None,
    text: str,
) -> SystemMessage:
    """Append text to a system message.

    Args:
        system_message: Existing system message or None.
        text: Text to add to the system message.

    Returns:
        New SystemMessage with the text appended.
    """
    new_content: list[ContentBlock] = list(system_message.content_blocks) if system_message else []
    if new_content:
        text = f"\n\n{text}"
    new_content.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=new_content)


VOLATILE_CONTEXT_KEY = "volatile_context"
"""``additional_kwargs`` flag on the per-call context message placed by
:func:`place_volatile_context_message`. Consumers that reason about the
"real" conversation (e.g. the prompt-caching breakpoint) skip it."""


def place_volatile_context_message(
    messages: list[AnyMessage],
    text: str,
) -> list[AnyMessage]:
    """Place ``text`` as a flagged :class:`HumanMessage` just before the current step.

    For volatile, per-call context (the on-screen ``<current_page>`` /
    ``<client_objects>`` block), applied to the model request only, never
    checkpointed. "The current step" is the last model call that called tools, and
    their results, when it comes after the last user message; otherwise (a turn's
    first step) the block goes last, after the user's message.

    Not last: after the tool results, a user message reads as a NEW ask. Replaying a
    captured console request (set a field and save, then Reject), with the block last
    the model acted on the page again instead of answering: Claude Sonnet 4.6 re-sent
    the rejected save 5/5, Haiku 4.5 2/5, and gpt-6-sol read or highlighted the page
    9/9. Placed before the step, all three answered (5/5, 5/5, 5/5).

    Not after the user's message either: the block is not persisted, so wherever it
    sits, the next request's token stream diverges there. Right after the user's
    message, every step that changed it and every next turn re-sent the whole tool
    loop past the provider prompt cache. Before the current step, a call re-sends one
    step (the previous call and its results); a new turn, only the last step of the
    previous one. On Anthropic/Bedrock that needs a breakpoint in front of the block,
    which ``LiteLLMPromptCachingMiddleware`` writes (``_tag_before_current_step``).

    Role validity: ``[..., Tool, Human(block), AI, Tool]`` and ``[..., Human,
    Human(block)]`` are valid chat-completions requests, and the Anthropic/Bedrock
    adapters fold consecutive user-role messages (tool results are user-role there)
    into one turn.
    """
    block = HumanMessage(content=text, additional_kwargs={VOLATILE_CONTEXT_KEY: True})
    at = current_step_start(messages)
    return [*messages[:at], block, *messages[at:]]


def current_step_start(messages: list[AnyMessage]) -> int:
    """The index of the last tool-calling model call when no user message follows it,
    else the end (a call that only answered is not a step in progress)."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            return i if messages[i].tool_calls else len(messages)
        if isinstance(messages[i], HumanMessage):
            break
    return len(messages)
