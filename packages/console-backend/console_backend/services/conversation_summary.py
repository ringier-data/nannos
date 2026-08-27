"""An LLM-written title and one-line summary for a conversation.

Until now a conversation was named after the first 100 characters the user
typed. That identifies it for the person who just typed it and for nobody after
that — a list of "can you check whether the pacing on the campaign we discussed
la" tells you nothing. After the first exchange finishes we ask the cheap chat
tier for a short label plus one sentence about what the conversation is for, and
store both on the conversation row.

Rules this module keeps:

* Best effort. No model configured, a gateway error, an unreadable answer — the
  existing title stays and nothing is raised. A conversation list is not worth
  breaking a turn over.
* Once per conversation. `metadata.title_source == "llm"` is the guard, so a
  restart, a second turn or a retry does not pay for it again. A first turn that
  ended in an approval prompt (no answer to summarize) leaves the flag unset, so
  the next completed turn picks it up.
* The user's name wins. `title_source == "user"` means they renamed it
  themselves; we write the summary under that name if it has none yet, and never
  touch the title.
* Off the turn's critical path. The caller fires this as a background task after
  the answer is already saved and streamed.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..db.connection import get_async_session_factory
from .llm_gateway import gateway_chat

logger = logging.getLogger(__name__)

#: Marks a conversation whose title came from the model — the run-once guard.
TITLE_SOURCE_LLM = "llm"

#: Marks a conversation the USER named (the rename endpoint). Their name stands;
#: this module may still write the summary under it, but never the title.
TITLE_SOURCE_USER = "user"

#: How much of each side of the exchange the model sees. The first exchange sets
#: the subject; the tail of a long answer rarely changes it, and tokens here are
#: pure overhead on every new conversation.
MAX_INPUT_CHARS = 2_000

#: Hard caps on what we store, whatever the model returns.
MAX_TITLE_CHARS = 60
MAX_SUMMARY_CHARS = 200

_PROMPT = """You are labelling a saved conversation for a list in an ad-operations tool.

Read this exchange from it and answer with JSON only:

{{"title": "...", "summary": "..."}}

- title: at most 8 words, no quotes, no trailing period. Name the SUBJECT, not \
the act of asking — "Campaign 42 pacing shortfall", never "User asks about a campaign".
- summary: ONE sentence, at most 25 words, saying what the conversation is about \
and what the user wanted.
- Write both in the same language the user wrote in.
- Use the names, ids and numbers that actually appear. Invent nothing.

USER:
{user_text}

ASSISTANT:
{assistant_text}"""


async def resolve_summary_model() -> str | None:
    """The gateway alias for this call, or None when no chat default is set.

    Titling is high-volume and cost-sensitive, so it rides the cheap ``chat:low``
    fleet default and falls back to standard ``chat`` — the same model_defaults
    source of truth catalog summarization uses
    (``catalog.sync.resolve_summarization_alias``). None means "skip", not "fail":
    an unconfigured deployment keeps the first-message titles.
    """
    from ..repositories.model_defaults_repository import ModelDefaultsRepository

    try:
        session_factory = get_async_session_factory()
        async with session_factory() as db:
            defaults = await ModelDefaultsRepository().get_all(db)
        return defaults.get("chat:low") or defaults.get("chat")
    except Exception as e:
        logger.warning("Could not read model defaults for conversation titling: %s", e)
        return None


def parse_summary_response(raw: str) -> tuple[str, str] | None:
    """Read ``{"title", "summary"}`` out of a completion, or None if it isn't there.

    Tolerates the two things models do to JSON: wrap it in a ``` fence, and add a
    sentence before or after it. Both fields must survive trimming — half an
    answer is not worth overwriting a title with.
    """
    if not raw or not raw.strip():
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    title = data.get("title")
    summary = data.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        return None
    title = " ".join(title.split()).strip(' "\'').rstrip(".")[:MAX_TITLE_CHARS].strip()
    summary = " ".join(summary.split()).strip(' "\'')[:MAX_SUMMARY_CHARS].strip()
    if not title or not summary:
        return None
    return title, summary


#: Placeholder assistant rows the persistence layer writes for a bare status or
#: task event — "Status: completed at 2026-…", "Task working: t-1" (see
#: messages_service._parse_agent_response / _parse_task). They are not answers,
#: and titling a conversation from one would burn the single attempt we get.
#: Anchored at both ends on purpose: a real answer that merely OPENS with the word
#: "Status:" must still count as an answer.
_PLACEHOLDER_ANSWER = re.compile(
    r"^(status:\s*[a-z_-]+(\s+at\s+\S+)?|task\s+[a-z_-]+:\s*\S*)$",
    re.IGNORECASE,
)


def usable_answer(text: str | None) -> str:
    """The answer worth summarizing, or '' when there is none.

    Guards the one shot: a placeholder would set the run-once flag and lock in a
    title written from the words "Status: completed".
    """
    cleaned = " ".join((text or "").split())
    if not cleaned or _PLACEHOLDER_ANSWER.match(cleaned):
        return ""
    return cleaned[:MAX_INPUT_CHARS]


async def generate_summary(user_text: str, assistant_text: str, *, user_sub: str | None = None) -> tuple[str, str] | None:
    """Ask the model for (title, summary). None on any failure — callers skip."""
    model = await resolve_summary_model()
    if not model:
        # WARNING, not debug: this is the one path that skips silently, and it is
        # the likeliest reason a deployment sees no generated titles. The chat model
        # a turn uses comes from the user's own settings, so `model_defaults` can be
        # empty in an otherwise perfectly working environment.
        logger.warning(
            "No 'chat:low' or 'chat' model default is set — conversations keep their "
            "first-message titles. An admin sets these in the console's model defaults."
        )
        return None
    prompt = _PROMPT.format(user_text=user_text, assistant_text=assistant_text)
    try:
        raw = await gateway_chat(
            prompt,
            model=model,
            # Thinking off. Naming a conversation from two short strings needs no
            # reasoning, and thinking tokens are billed like any other generated
            # token — on every new conversation. Turning them off is the whole
            # saving here; it also cuts the latency that made this a background task.
            reasoning_effort="none",
            # Kept high on purpose, as the fallback for when the line above does not
            # take: a reasoning model spends completion tokens on thinking BEFORE any
            # text, and 200 left the JSON truncated or empty. An unused ceiling is
            # free (billing is per generated token, and the timeout bounds latency).
            max_tokens=4000,
            # The gateway attributes cost by OIDC subject; without it nothing is logged.
            metadata={"user_sub": user_sub} if user_sub else None,
            timeout=20.0,
        )
    except Exception as e:
        logger.warning("Conversation titling call failed: %s", e)
        return None
    parsed = parse_summary_response(raw)
    if parsed is None:
        logger.warning(
            "Conversation titling response unparseable (model=%s, %d chars): %.200r",
            model,
            len(raw),
            raw,
        )
    return parsed


async def maybe_summarize_conversation(
    conversation_service: Any,
    conversation_id: str,
    user_id: str,
    *,
    answer: str,
    user_sub: str | None = None,
    on_stored: Callable[[str, str], Awaitable[None]] | None = None,
) -> bool:
    """Title and summarize a conversation once, from the answer just produced.

    Both halves arrive without touching the messages table. The QUESTION is the
    conversation's own title, which creation set to the opening user message. The
    ANSWER is passed in by the caller, which has just streamed it.

    That is the fix for the first version, which read the exchange back out of the
    messages table: it had to guess at stored roles, part shapes and synthetic
    status rows, and it raced the very writes that produce them.

    `on_stored(title, summary)` runs after a successful write — the caller uses it
    to push the new name to the conversation's viewers. Keeping it a callback is
    what lets this module stay free of socket plumbing.

    Returns True when a summary was stored. Swallows everything: this runs as a
    detached background task, so an exception here would only surface as an
    unretrieved-task warning at shutdown.
    """
    try:
        conversation = await conversation_service.get_conversation(conversation_id, user_id=user_id)
        if not conversation:
            return False
        metadata = conversation.metadata or {}
        title_source = metadata.get("title_source")
        if title_source == TITLE_SOURCE_LLM:
            return False
        # A conversation the user named keeps that name for good. It is still
        # worth one run for the SUMMARY — the second line of every list row —
        # but only while it has none; after that there is nothing left to write.
        named_by_user = title_source == TITLE_SOURCE_USER
        if named_by_user and metadata.get("summary"):
            return False

        # The title is the opening user message (creation set it) — or, for a
        # conversation the user renamed, the name they chose. Either names the
        # subject well enough to summarize against.
        question = " ".join((conversation.title or "").split())[:MAX_INPUT_CHARS]
        reply = usable_answer(answer)
        if not question or not reply:
            logger.info(
                "Nothing to title conversation %s from: question=%d chars, answer=%d chars",
                conversation_id,
                len(question),
                len(reply),
            )
            return False

        generated = await generate_summary(question, reply, user_sub=user_sub)
        if not generated:
            return False

        title, summary = generated
        if named_by_user:
            # Keep the name the user typed, and keep its protection: writing
            # 'llm' here would make the next rename overwritable again.
            title = conversation.title or title
        stored = await conversation_service.update_summary(
            conversation_id,
            user_id,
            title=title,
            summary=summary,
            title_source=TITLE_SOURCE_USER if named_by_user else TITLE_SOURCE_LLM,
        )
        if stored:
            logger.info("Titled conversation %s: %s", conversation_id, title)
            if on_stored:
                await on_stored(title, summary)
        return stored
    except Exception as e:
        logger.warning("Conversation summary for %s failed: %s", conversation_id, e)
        return False
