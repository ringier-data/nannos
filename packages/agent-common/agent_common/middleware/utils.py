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


VOLATILE_CONTEXT_KEY = "volatile_context"
"""``additional_kwargs`` flag on a per-call context message appended by
:func:`append_volatile_context_message`. Consumers that reason about the
"real" conversation tail (e.g. the prompt-caching breakpoint) skip it."""


def append_volatile_context_message(
    messages: list[AnyMessage],
    text: str,
) -> list[AnyMessage]:
    """Append ``text`` as a trailing, flagged :class:`HumanMessage`.

    For volatile, per-call context (the on-screen ``<current_page>`` /
    ``<client_objects>`` block). The block is applied to the model request only —
    never checkpointed — so the ONLY placement that keeps the provider prompt cache
    warm is *after* everything that is persisted:

    - Appending to the last human message (the previous design) moved the block
      from ``Human_N`` to ``Human_N+1`` on the next turn. ``Human_N`` was then sent
      WITHOUT the block it carried before, so the token stream diverged there and
      turn N's whole tool loop was re-tokenised on every subsequent turn, page
      changed or not.
    - Appended last, every byte before the block is identical to what the
      checkpoint holds, across tool-loop iterations and across turns. Only the
      block itself is re-tokenised per call.

    Role validity: mid-tool-loop the tail is a ``ToolMessage``. Chat-completions
    accepts a user message after tool results, and the Anthropic/Bedrock adapters
    fold consecutive user-role messages (tool results are user-role there) into one
    turn, so ``[..., Tool, Human(block)]`` is a valid request everywhere we route.

    The message is flagged ``additional_kwargs[VOLATILE_CONTEXT_KEY] = True`` so the
    caching middleware places its conversation breakpoint on the stable message in
    front of it rather than on the block.
    """
    return [*messages, HumanMessage(content=text, additional_kwargs={VOLATILE_CONTEXT_KEY: True})]
