"""Who may answer a pending interrupt.

A conversation's pending interrupt (a tool approval, an in-task authorization)
belongs to the speaker whose turn raised it. In a channel conversation, where
one checkpoint serves every participant, the next message can come from anyone;
only the owner's may be read as the answer.

No bookkeeping is needed to know the owner. LangGraph copies the run config's
scalar ``metadata`` into every checkpoint it writes, and the executor puts the
speaker's ``user_id`` and ``user_name`` there, so the checkpoint that holds the
interrupt already names whose turn wrote it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InterruptOwner:
    user_id: str
    user_name: str | None


def interrupt_owner(state: Any) -> InterruptOwner | None:
    """The speaker whose turn wrote *state*'s checkpoint, or None when unrecorded.

    None means "don't know", not "nobody": the caller lets the message through
    rather than locking a conversation it cannot attribute.
    """
    metadata = getattr(state, "metadata", None) or {}
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    user_name = metadata.get("user_name")
    return InterruptOwner(user_id=user_id, user_name=user_name if isinstance(user_name, str) and user_name else None)


def foreign_interrupt_message(owner_name: str | None) -> str:
    """The reply to someone who wrote while another speaker's interrupt is pending."""
    who = owner_name or "the person who started it"
    return (
        f"This thread is waiting for {who} to answer a pending request, and only they can answer it. "
        "Mention me outside this thread to start a new conversation."
    )
