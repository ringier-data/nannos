"""Tests for billing unit breakdown extraction from LangChain usage metadata."""

from ringier_a2a_sdk.cost_tracking.callback import CostTrackingCallback


class TestBillingUnitExtraction:
    """Tests for _extract_billing_unit_breakdown with real-world provider examples."""

    def test_openai_with_cache_read(self):
        """Test OpenAI usage metadata with cache_read in input_token_details."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 7879,
            "output_tokens": 54,
            "total_tokens": 7933,
            "input_token_details": {
                "audio": 0,
                "cache_read": 5248,
            },
            "output_token_details": {
                "audio": 0,
                "reasoning": 0,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # Expected: base_input = 7879 - 5248 = 2631
        assert breakdown["base_input_tokens"] == 2631
        assert breakdown["cache_read_input_tokens"] == 5248
        assert breakdown["base_output_tokens"] == 54
        # audio and reasoning are zero, should not be included
        assert "audio_input_tokens" not in breakdown
        assert "audio_output_tokens" not in breakdown
        assert "reasoning_output_tokens" not in breakdown

    def test_bedrock_with_cache_details(self):
        """Test Bedrock usage metadata with cache_creation and cache_read."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 14514,
            "output_tokens": 79,
            "total_tokens": 14593,
            "input_token_details": {
                "cache_creation": 0,
                "cache_read": 0,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # No cache usage, all tokens are base
        assert breakdown["base_input_tokens"] == 14514
        assert breakdown["base_output_tokens"] == 79
        assert "cache_creation_input_tokens" not in breakdown
        assert "cache_read_input_tokens" not in breakdown

    def test_bedrock_with_active_cache(self):
        """Test Bedrock with active cache usage."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 10000,
            "output_tokens": 500,
            "total_tokens": 10500,
            "input_token_details": {
                "cache_creation": 2000,
                "cache_read": 3000,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # base_input = 10000 - 2000 - 3000 = 5000
        assert breakdown["base_input_tokens"] == 5000
        assert breakdown["cache_creation_input_tokens"] == 2000
        assert breakdown["cache_read_input_tokens"] == 3000
        assert breakdown["base_output_tokens"] == 500

    def test_openai_with_audio_and_reasoning(self):
        """Test OpenAI with audio and reasoning tokens."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 350,
            "output_tokens": 240,
            "total_tokens": 590,
            "input_token_details": {
                "audio": 10,
                "cache_creation": 200,
                "cache_read": 100,
            },
            "output_token_details": {
                "audio": 10,
                "reasoning": 200,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # base_input = 350 - 10 - 200 - 100 = 40
        assert breakdown["base_input_tokens"] == 40
        assert breakdown["audio_input_tokens"] == 10
        assert breakdown["cache_creation_input_tokens"] == 200
        assert breakdown["cache_read_input_tokens"] == 100

        # base_output = 240 - 10 - 200 = 30
        assert breakdown["base_output_tokens"] == 30
        assert breakdown["audio_output_tokens"] == 10
        assert breakdown["reasoning_output_tokens"] == 200

    def test_legacy_openai_format(self):
        """Test legacy OpenAI format with prompt_tokens and completion_tokens."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # No details provided, all tokens are base
        assert breakdown["base_input_tokens"] == 1000
        assert breakdown["base_output_tokens"] == 500

    def test_zero_tokens_excluded(self):
        """Test that zero-count billing units are not included in breakdown."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 0,
            "output_tokens": 100,
            "input_token_details": {
                "cache_read": 0,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # Only non-zero tokens should be present
        assert "base_input_tokens" not in breakdown
        assert "cache_read_input_tokens" not in breakdown
        assert breakdown["base_output_tokens"] == 100

    def test_details_exceed_total_additive_provider(self):
        """Test when details exceed total — provider reports details as additive (e.g. Bedrock caching)."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_token_details": {
                "cache_read": 150,  # More than total — details are additive, not included
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # total IS the base since details are additive
        assert breakdown["base_input_tokens"] == 100
        assert breakdown["cache_read_input_tokens"] == 150
        assert breakdown["base_output_tokens"] == 50

    def test_bedrock_prompt_caching_real_world(self):
        """Test real-world Bedrock prompt caching where input_tokens is non-cached only."""
        callback = CostTrackingCallback(cost_logger=None)

        # Actual pattern from logs: input_tokens=1225, cache_read=78440
        usage_metadata = {
            "input_tokens": 1225,
            "output_tokens": 176,
            "total_tokens": 1401,
            "input_token_details": {
                "cache_read": 78440,
                "cache_creation": 0,
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        assert breakdown["base_input_tokens"] == 1225
        assert breakdown["cache_read_input_tokens"] == 78440
        assert breakdown["base_output_tokens"] == 176
        assert "cache_creation_input_tokens" not in breakdown

    def test_custom_provider_fields(self):
        """Test that custom provider-specific fields in token details are handled."""
        callback = CostTrackingCallback(cost_logger=None)

        usage_metadata = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_token_details": {
                "custom_cache_type": 200,  # Hypothetical custom field
            },
            "output_token_details": {
                "custom_reasoning": 100,  # Hypothetical custom field
            },
        }

        breakdown = callback._extract_billing_unit_breakdown(usage_metadata, {})

        # Custom fields should be emitted with standard naming
        assert breakdown["base_input_tokens"] == 800  # 1000 - 200
        assert breakdown["custom_cache_type_input_tokens"] == 200
        assert breakdown["base_output_tokens"] == 400  # 500 - 100
        assert breakdown["custom_reasoning_output_tokens"] == 100
