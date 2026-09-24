"""The rules moved to the SDK; the old import path still serves them."""

from agent_common.core import message_formatting as via_agent_common
from ringier_a2a_sdk import message_formatting as sdk


def test_agent_common_serves_the_sdk_objects():
    assert via_agent_common.FORMATTING_RULES is sdk.FORMATTING_RULES
    assert via_agent_common.formatting_prompt_block is sdk.formatting_prompt_block
    assert via_agent_common.normalize_message_formatting is sdk.normalize_message_formatting
