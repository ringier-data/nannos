"""Utility functions for middleware."""

from langchain_core.messages import AnyMessage, ContentBlock, HumanMessage, SystemMessage


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


def append_to_last_human_message(
    messages: list[AnyMessage],
    text: str,
) -> list[AnyMessage] | None:
    """Append ``text`` to the content of the last :class:`HumanMessage`.

    Volatile, per-turn context (e.g. the on-screen ``<client_objects>`` manifest)
    belongs on the human turn rather than the system prompt: the system prompt and
    all prior conversation history stay byte-stable across turns, so the cached
    prefix survives while the volatile block rides the (already-uncached) tail.

    The last human message — not the literal last message — is targeted so the
    injection stays valid mid-tool-loop (where the tail is a ``ToolMessage``) and
    on HITL resume (where the tail is an interrupted AI/tool message).

    Args:
        messages: The model request's message list.
        text: Text to append to the last human message.

    Returns:
        A new message list with ``text`` appended to the last human message, or
        ``None`` if no human message is present (so callers can fall back to
        system-prompt injection).
    """
    idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    if idx is None:
        return None

    target = messages[idx]
    content = target.content
    if isinstance(content, str):
        new_content: str | list = f"{content}\n\n{text}" if content else text
    elif isinstance(content, list):
        new_content = [*content, {"type": "text", "text": text}]
    else:
        new_content = text

    new_messages = list(messages)
    new_messages[idx] = target.model_copy(update={"content": new_content})
    return new_messages
