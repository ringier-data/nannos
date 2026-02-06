"""Tests for usage router security (authentication and authorization)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import status


@pytest.mark.asyncio
class TestUsageLogSecuritySingle:
    """Test security for single usage log endpoint."""

    async def test_log_usage_uses_current_user_id(self, client_with_db, test_user, pg_session):
        """Test that /log endpoint uses current_user.id regardless of request body user_id."""

        async def mock_require_auth_or_bearer_token(request):
            request.state.user = test_user
            return test_user

        with patch(
            "playground_backend.routers.usage_router.require_auth_or_bearer_token",
            side_effect=mock_require_auth_or_bearer_token,
        ):
            response = await client_with_db.post(
                "/api/v1/usage/log",
                json={
                    "user_id": "any-user-id",  # This is ignored by the router
                    "provider": "bedrock_converse",
                    "model_name": "claude-sonnet-4",
                    "billing_unit_breakdown": {"input_tokens": 100, "output_tokens": 50},
                    "invoked_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            assert response.status_code == status.HTTP_201_CREATED
            assert response.json()["status"] == "logged"

    # Removed: test_log_usage_rejects_mismatched_user_id
    # Router now uses current_user.id directly, ignoring request body user_id

    async def test_log_usage_accepts_matching_user_id(self, client_with_db, test_user, pg_session):
        """Test that /log endpoint accepts when log user_id matches token sub."""

        async def mock_require_auth_or_bearer_token(request):
            request.state.user = test_user
            return test_user

        with patch(
            "playground_backend.routers.usage_router.require_auth_or_bearer_token",
            side_effect=mock_require_auth_or_bearer_token,
        ):
            response = await client_with_db.post(
                "/api/v1/usage/log",
                json={
                    "user_id": test_user.id,  # Matches token sub
                    "provider": "bedrock_converse",
                    "model_name": "claude-sonnet-4",
                    "billing_unit_breakdown": {"input_tokens": 100, "output_tokens": 50},
                    "invoked_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            assert response.status_code == status.HTTP_201_CREATED
            assert response.json()["status"] == "logged"
            assert "id" in response.json()


@pytest.mark.asyncio
class TestUsageLogSecurityBatch:
    """Test security for batch usage log endpoint."""

    async def test_batch_log_uses_current_user_id(self, client_with_db, test_user, pg_session):
        """Test that /batch-log endpoint uses current_user.id regardless of request body user_ids."""

        async def mock_require_auth_or_bearer_token(request):
            request.state.user = test_user
            return test_user

        with patch(
            "playground_backend.routers.usage_router.require_auth_or_bearer_token",
            side_effect=mock_require_auth_or_bearer_token,
        ):
            response = await client_with_db.post(
                "/api/v1/usage/batch-log",
                json={
                    "logs": [
                        {
                            "user_id": "any-user-id",  # Ignored by router
                            "provider": "bedrock_converse",
                            "model_name": "claude-sonnet-4",
                            "billing_unit_breakdown": {"input_tokens": 100, "output_tokens": 50},
                            "invoked_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ]
                },
            )
            assert response.status_code == status.HTTP_201_CREATED
            assert response.json()["status"] == "logged"

    # Removed: test_batch_log_rejects_mismatched_user_id_single
    # Router now uses current_user.id directly, ignoring request body user_ids

    # Removed: test_batch_log_rejects_mixed_user_ids
    # Router now uses current_user.id directly, ignoring request body user_ids

    async def test_batch_log_accepts_matching_user_ids(self, client_with_db, test_user, pg_session):
        """Test that /batch-log accepts when all log user_ids match token sub."""

        async def mock_require_auth_or_bearer_token(request):
            request.state.user = test_user
            return test_user

        with patch(
            "playground_backend.routers.usage_router.require_auth_or_bearer_token",
            side_effect=mock_require_auth_or_bearer_token,
        ):
            response = await client_with_db.post(
                "/api/v1/usage/batch-log",
                json={
                    "logs": [
                        {
                            "user_id": test_user.id,  # Matches
                            "provider": "bedrock_converse",
                            "model_name": "claude-sonnet-4",
                            "billing_unit_breakdown": {"input_tokens": 100, "output_tokens": 50},
                            "invoked_at": datetime.now(timezone.utc).isoformat(),
                        },
                        {
                            "user_id": test_user.id,  # Also matches
                            "provider": "bedrock_converse",
                            "model_name": "claude-sonnet-4",
                            "billing_unit_breakdown": {"input_tokens": 50, "output_tokens": 25},
                            "invoked_at": datetime.now(timezone.utc).isoformat(),
                        },
                    ]
                },
            )
            assert response.status_code == status.HTTP_201_CREATED
            assert response.json()["status"] == "logged"
            assert response.json()["count"] == 2
            assert len(response.json()["ids"]) == 2
