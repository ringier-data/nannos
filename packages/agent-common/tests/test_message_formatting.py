"""The channel decides how a message is written, and the rules are stated once."""

from agent_common.core.message_formatting import (
    formatting_prompt_block,
    formatting_rules,
    normalize_message_formatting,
)


class TestNormalize:
    def test_known_formats_pass_through(self):
        for value in ("markdown", "slack", "google-chat", "plain"):
            assert normalize_message_formatting(value) == value

    def test_case_and_whitespace_are_forgiven(self):
        assert normalize_message_formatting(" Slack ") == "slack"

    def test_unknown_and_non_strings_fall_back_to_markdown(self):
        # Metadata arrives from A2A clients and, over gRPC, through a protobuf Struct:
        # a stray type must not raise inside a scheduled run.
        for value in (None, "", "html", 3, True, {"slack": True}):
            assert normalize_message_formatting(value) == "markdown"


class TestPromptBlock:
    def test_slack_gets_mrkdwn_rules_and_a_warning_off_markdown(self):
        block = formatting_prompt_block("slack")
        assert block.startswith('<message_formatting format="slack">')
        assert "mrkdwn" in block
        assert "**bold**" in block  # named as what to avoid
        assert block.endswith("</message_formatting>")

    def test_google_chat_gets_its_own_markup(self):
        block = formatting_prompt_block("google-chat")
        assert '<message_formatting format="google-chat">' in block
        assert "~strikethrough~" in block

    def test_markdown_says_nothing(self):
        # Standard behaviour; an instruction saying so only spends tokens.
        assert formatting_prompt_block("markdown") == ""
        assert formatting_rules("markdown") == ""

    def test_an_unknown_format_says_nothing_rather_than_guessing(self):
        assert formatting_prompt_block("teams") == ""
