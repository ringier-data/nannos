"""Channel-dependent message formatting instructions.

The text an agent writes is rendered by whatever client delivers it, and those
renderers disagree: Slack reads mrkdwn (``*bold*``, no headers), Google Chat its
own markup, a web console real Markdown. So the format is a property of the
DELIVERY CHANNEL, not of the agent — and the agent has to be told which one it
is writing for, because nothing downstream rewrites its output.

Interactive turns get this from the client's ``messageFormatting`` request
metadata. Scheduled runs have no client on the other end: the format comes from
the delivery channel the job notifies, is carried in the dispatch metadata under
the same key, and lands here. One table so a channel's rules are stated once and
every writer (orchestrator turn, scheduled sub-agent, scheduler-written
notification) obeys the same ones.
"""

from __future__ import annotations

from typing import Literal, get_args

MessageFormatting = Literal["markdown", "slack", "google-chat", "plain"]

DEFAULT_MESSAGE_FORMATTING: MessageFormatting = "markdown"

KNOWN_FORMATS = frozenset(get_args(MessageFormatting))

#: Rendering rules per channel, as prose for the model. ``markdown`` is absent on
#: purpose: it is standard behaviour and an instruction saying so only spends tokens.
FORMATTING_RULES: dict[str, str] = {
    "slack": (
        "Format responses using Slack mrkdwn syntax: *bold* for emphasis, _italic_ for secondary "
        "emphasis, `code` for inline code, ```code blocks``` for multi-line code. "
        "Use <https://url|label> for links and a leading '• ' for bullets. "
        "Avoid markdown syntax that Slack doesn't support (e.g. # headers, **bold**, "
        "[links](url), | tables |)."
    ),
    "google-chat": (
        "Format responses using Google Chat markup syntax: *bold* for emphasis, _italic_ for secondary "
        "emphasis, ~strikethrough~ for strikethrough, `code` for inline code, ```code blocks``` for "
        "multi-line code. Use plain URLs for links (they are auto-linked). "
        "Avoid markdown syntax that Google Chat doesn't support (e.g. # headers, **bold**, "
        "[links](url), | tables |)."
    ),
    "plain": (
        "Use plain text only. Do not use any formatting syntax "
        "(no markdown, no bold, no code blocks). Keep responses simple and readable."
    ),
}


def normalize_message_formatting(value: object) -> MessageFormatting:
    """Coerce a metadata value to a known format, defaulting to ``markdown``.

    Metadata reaches us from A2A clients and, over gRPC, through a protobuf Struct, so
    an unexpected type or a channel format written by an older client must degrade to
    the default rather than raise inside a scheduled run.
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in KNOWN_FORMATS:
            return candidate  # type: ignore[return-value]
    return DEFAULT_MESSAGE_FORMATTING


def formatting_rules(value: object) -> str:
    """The bare rules sentence for a format, or ``""`` for plain Markdown."""
    return FORMATTING_RULES.get(normalize_message_formatting(value), "")


def formatting_prompt_block(value: object) -> str:
    """The rules as a ``<message_formatting>`` block, or ``""`` when none are needed.

    Returned without surrounding blank lines; callers place it (system-prompt
    addendum, user-preferences block, one-shot prompt) as they need.
    """
    fmt = normalize_message_formatting(value)
    rules = FORMATTING_RULES.get(fmt)
    if not rules:
        return ""
    return f'<message_formatting format="{fmt}">\n{rules}\n</message_formatting>'
