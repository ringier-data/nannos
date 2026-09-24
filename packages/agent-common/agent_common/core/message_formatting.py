"""Channel-dependent message formatting instructions.

Re-exported from the SDK, where the rules live: ``messageFormatting`` is A2A wire
metadata and console-backend writes notifications under the same rules without
depending on agent-common. Existing imports from this path keep working.
"""

from ringier_a2a_sdk.message_formatting import (
    DEFAULT_MESSAGE_FORMATTING,
    FORMATTING_RULES,
    KNOWN_FORMATS,
    MessageFormatting,
    formatting_prompt_block,
    formatting_rules,
    normalize_message_formatting,
)

__all__ = [
    "DEFAULT_MESSAGE_FORMATTING",
    "FORMATTING_RULES",
    "KNOWN_FORMATS",
    "MessageFormatting",
    "formatting_prompt_block",
    "formatting_rules",
    "normalize_message_formatting",
]
