"""Tests for usage tracking validation including custom billing units."""

from datetime import datetime, timezone

import pytest
from console_backend.models.usage import UsageLogCreate
from pydantic import ValidationError


class TestBillingUnitValidation:
    """Test validation of billing unit names in billing_unit_breakdown."""

    def test_valid_standard_token_types(self):
        """Test that standard LLM token types are accepted."""
        log = UsageLogCreate(
            provider="openai",
            model_name="gpt-4o",
            billing_unit_breakdown={
                "input_tokens": 1234,
                "output_tokens": 567,
                "cache_read_input_tokens": 100,
                "reasoning_tokens": 50,
            },
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown["input_tokens"] == 1234
        assert log.billing_unit_breakdown["output_tokens"] == 567

    def test_valid_custom_billing_units(self):
        """Test that custom billing units with valid names are accepted."""
        log = UsageLogCreate(
            provider="my_service",
            model_name="api-v2",
            billing_unit_breakdown={
                "premium_api_calls": 5,
                "standard_api_calls": 10,
                "vector_searches": 15,
                "documents_indexed": 100,
                "gpu_seconds": 30,
            },
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown["premium_api_calls"] == 5
        assert log.billing_unit_breakdown["vector_searches"] == 15

    def test_valid_snake_case_patterns(self):
        """Test various valid snake_case patterns."""
        valid_names = [
            "api_calls",
            "requests_tier1",
            "search_v2",
            "storage_gb_hours",
            "function_executions_premium",
            "doc123",
            "v2_requests",
        ]

        for name in valid_names:
            log = UsageLogCreate(
                provider="test",
                model_name="test",
                billing_unit_breakdown={name: 1},
                invoked_at=datetime.now(timezone.utc),
            )
            assert log.billing_unit_breakdown[name] == 1

    def test_reject_invalid_formats(self):
        """Test that invalid formats are rejected."""
        invalid_cases = [
            ("Premium API Calls", "spaces"),
            ("apiCalls", "camelCase"),
            ("API-Calls", "hyphens"),
            ("123_requests", "starts with number"),
            ("_requests", "starts with underscore"),
            ("requests_", "ends with underscore"),
            ("", "empty string"),
            ("ab", "too short"),
        ]

        for invalid_name, reason in invalid_cases:
            # Different validation errors for different issues
            with pytest.raises(ValidationError):
                UsageLogCreate(
                    provider="test",
                    model_name="test",
                    billing_unit_breakdown={invalid_name: 1},
                    invoked_at=datetime.now(timezone.utc),
                )

    def test_reject_reserved_names(self):
        """Test that reserved billing unit names are rejected."""
        reserved_names = ["id", "cost", "total", "timestamp", "count"]

        for reserved in reserved_names:
            with pytest.raises(ValidationError, match="is reserved"):
                UsageLogCreate(
                    provider="test",
                    model_name="test",
                    billing_unit_breakdown={reserved: 1},
                    invoked_at=datetime.now(timezone.utc),
                )

    def test_reject_too_long_names(self):
        """Test that names exceeding 64 characters are rejected."""
        too_long_name = "a" * 65

        with pytest.raises(ValidationError, match="must be between 3 and 64 characters"):
            UsageLogCreate(
                provider="test",
                model_name="test",
                billing_unit_breakdown={too_long_name: 1},
                invoked_at=datetime.now(timezone.utc),
            )

    def test_reject_zero_or_negative_counts(self):
        """Test that zero or negative unit counts are rejected."""
        with pytest.raises(ValidationError, match="must be positive"):
            UsageLogCreate(
                provider="test",
                model_name="test",
                billing_unit_breakdown={"input_tokens": 0},
                invoked_at=datetime.now(timezone.utc),
            )

        with pytest.raises(ValidationError, match="must be positive"):
            UsageLogCreate(
                provider="test",
                model_name="test",
                billing_unit_breakdown={"input_tokens": -100},
                invoked_at=datetime.now(timezone.utc),
            )

    def test_mixed_token_and_custom_units(self):
        """Test that token types and custom billing units can coexist."""
        log = UsageLogCreate(
            provider="hybrid_service",
            model_name="llm-with-tools",
            billing_unit_breakdown={
                "input_tokens": 1000,
                "output_tokens": 500,
                "tool_calls": 5,
                "api_requests": 3,
                "cache_hits": 200,
            },
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown["input_tokens"] == 1000
        assert log.billing_unit_breakdown["tool_calls"] == 5
        assert log.billing_unit_breakdown["api_requests"] == 3

    def test_edge_case_minimum_length(self):
        """Test minimum valid length (3 characters)."""
        log = UsageLogCreate(
            provider="test",
            model_name="test",
            billing_unit_breakdown={"abc": 1},
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown["abc"] == 1

    def test_edge_case_maximum_length(self):
        """Test maximum valid length (64 characters)."""
        max_length_name = "a" * 64
        log = UsageLogCreate(
            provider="test",
            model_name="test",
            billing_unit_breakdown={max_length_name: 1},
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown[max_length_name] == 1

    def test_case_sensitivity(self):
        """Test that uppercase letters are rejected (must be lowercase)."""
        with pytest.raises(ValidationError, match="Invalid billing unit name"):
            UsageLogCreate(
                provider="test",
                model_name="test",
                billing_unit_breakdown={"Input_Tokens": 1},
                invoked_at=datetime.now(timezone.utc),
            )

    def test_multiple_underscores(self):
        """Test that multiple consecutive underscores are allowed."""
        log = UsageLogCreate(
            provider="test",
            model_name="test",
            billing_unit_breakdown={"api__calls__v2": 1},
            invoked_at=datetime.now(timezone.utc),
        )
        assert log.billing_unit_breakdown["api__calls__v2"] == 1
