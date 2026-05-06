"""Tests for phone verification service and auth router endpoints."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from twilio.base.exceptions import TwilioRestException

from console_backend.services.phone_verification_service import (
    PhoneVerificationService,
)

# ── E164 validation ────────────────────────────────────────────────────────────


class TestE164Validation:
    """Tests for E.164 phone number format validation."""

    @pytest.mark.parametrize(
        "phone",
        [
            "+41791234567",
            "+1234567890",
            "+442071234567",
            "+861390000000",
            "+10",  # minimum: + country-code digit
        ],
    )
    def test_valid_e164(self, phone: str):
        assert PhoneVerificationService.validate_e164(phone) is True

    @pytest.mark.parametrize(
        "phone",
        [
            "41791234567",  # missing +
            "+0123456789",  # leading zero after +
            "+",  # just plus
            "",  # empty
            "not-a-number",  # random text
            "+1234567890123456",  # 16 digits — too long
            "+41 79 123 45 67",  # spaces
        ],
    )
    def test_invalid_e164(self, phone: str):
        assert PhoneVerificationService.validate_e164(phone) is False


# ── PhoneVerificationService ────────────────────────────────────────────────────


class TestPhoneVerificationService:
    """Tests for send_verification and check_verification."""

    @pytest.fixture()
    def service(self):
        svc = PhoneVerificationService()
        # Inject a mock Twilio client so we never hit real APIs
        svc._client = MagicMock()
        return svc

    @pytest.fixture()
    def mock_verify_service(self, service: PhoneVerificationService):
        """Return the mock verify v2 service chain."""
        return service._client.verify.v2.services.return_value

    # ── is_configured ──

    @patch("console_backend.services.phone_verification_service.config")
    def test_is_configured_true(self, mock_config):
        mock_config.twilio_verify.is_configured = True
        svc = PhoneVerificationService()
        assert svc.is_configured is True

    @patch("console_backend.services.phone_verification_service.config")
    def test_is_configured_false(self, mock_config):
        mock_config.twilio_verify.is_configured = False
        svc = PhoneVerificationService()
        assert svc.is_configured is False

    # ── send_verification ──

    @pytest.mark.asyncio
    async def test_send_verification_sms(self, service, mock_verify_service):
        mock_verify_service.verifications.create.return_value = MagicMock(status="pending")

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.send_verification("+41791234567", "sms")

        assert result is True
        mock_verify_service.verifications.create.assert_called_once_with(to="+41791234567", channel="sms")

    @pytest.mark.asyncio
    async def test_send_verification_call(self, service, mock_verify_service):
        mock_verify_service.verifications.create.return_value = MagicMock(status="pending")

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.send_verification("+41791234567", "call")

        assert result is True
        mock_verify_service.verifications.create.assert_called_once_with(to="+41791234567", channel="call")

    @pytest.mark.asyncio
    async def test_send_verification_invalid_phone(self, service):
        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            with pytest.raises(ValueError, match="Invalid E.164"):
                await service.send_verification("not-a-number")

    @pytest.mark.asyncio
    async def test_send_verification_invalid_channel(self, service):
        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            with pytest.raises(ValueError, match="Unsupported verification channel"):
                await service.send_verification("+41791234567", "email")

    @pytest.mark.asyncio
    async def test_send_verification_not_configured(self, service):
        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=False):
            with pytest.raises(RuntimeError, match="not configured"):
                await service.send_verification("+41791234567")

    @pytest.mark.asyncio
    async def test_send_verification_twilio_error(self, service, mock_verify_service):
        mock_verify_service.verifications.create.side_effect = TwilioRestException(
            status=400, uri="/test", msg="Bad request"
        )

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            with pytest.raises(TwilioRestException):
                await service.send_verification("+41791234567")

    @pytest.mark.asyncio
    async def test_send_verification_non_pending_status(self, service, mock_verify_service):
        mock_verify_service.verifications.create.return_value = MagicMock(status="failed")

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.send_verification("+41791234567")

        assert result is False

    # ── check_verification ──

    @pytest.mark.asyncio
    async def test_check_verification_approved(self, service, mock_verify_service):
        mock_verify_service.verification_checks.create.return_value = MagicMock(status="approved")

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.check_verification("+41791234567", "123456")

        assert result is True
        mock_verify_service.verification_checks.create.assert_called_once_with(to="+41791234567", code="123456")

    @pytest.mark.asyncio
    async def test_check_verification_rejected(self, service, mock_verify_service):
        mock_verify_service.verification_checks.create.return_value = MagicMock(status="pending")

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.check_verification("+41791234567", "000000")

        assert result is False

    @pytest.mark.asyncio
    async def test_check_verification_twilio_error(self, service, mock_verify_service):
        mock_verify_service.verification_checks.create.side_effect = TwilioRestException(
            status=404, uri="/test", msg="Not found"
        )

        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=True):
            result = await service.check_verification("+41791234567", "123456")

        assert result is False

    @pytest.mark.asyncio
    async def test_check_verification_not_configured(self, service):
        with patch.object(type(service), "is_configured", new_callable=PropertyMock, return_value=False):
            with pytest.raises(RuntimeError, match="not configured"):
                await service.check_verification("+41791234567", "123456")


# ── Auth Router Endpoints ────────────────────────────────────────────────────


class TestPhoneVerifyEndpoint:
    """Tests for POST /me/phone/verify."""

    @pytest.mark.asyncio
    async def test_send_verification_success(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True
            mock_svc.send_verification = AsyncMock(return_value=True)

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "+41791234567", "channel": "sms"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert data["phone_number"] == "+41791234567"

    @pytest.mark.asyncio
    async def test_send_verification_call_channel(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True
            mock_svc.send_verification = AsyncMock(return_value=True)

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "+41791234567", "channel": "call"},
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_send_verification_not_configured(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = False

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "+41791234567", "channel": "sms"},
            )

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_send_verification_invalid_phone(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "not-valid", "channel": "sms"},
            )

        assert response.status_code == 400
        assert "E.164" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_send_verification_invalid_channel(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "+41791234567", "channel": "email"},
            )

        assert response.status_code == 400
        assert "sms" in response.json()["detail"] or "call" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_send_verification_twilio_failure(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True
            mock_svc.send_verification = AsyncMock(return_value=False)

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/verify",
                json={"phone_number": "+41791234567", "channel": "sms"},
            )

        assert response.status_code == 500


class TestPhoneConfirmEndpoint:
    """Tests for POST /me/phone/confirm."""

    @pytest.mark.asyncio
    async def test_confirm_success(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True
            mock_svc.check_verification = AsyncMock(return_value=True)

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/confirm",
                json={"phone_number": "+41791234567", "code": "123456"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["phone_number_override"] == "+41791234567"
        assert data["phone_number"] == "+41791234567"

    @pytest.mark.asyncio
    async def test_confirm_wrong_code(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True
            mock_svc.check_verification = AsyncMock(return_value=False)

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/confirm",
                json={"phone_number": "+41791234567", "code": "000000"},
            )

        assert response.status_code == 400
        assert "Invalid or expired" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_confirm_not_configured(self, client_with_db):
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = False

            response = await client_with_db.post(
                "/api/v1/auth/me/phone/confirm",
                json={"phone_number": "+41791234567", "code": "123456"},
            )

        assert response.status_code == 503


class TestDeletePhoneOverride:
    """Tests for DELETE /me/phone/override endpoint."""

    @pytest.mark.asyncio
    async def test_clear_override_success(self, client_with_db):
        """Clearing phone_number_override returns updated phone fields."""
        response = await client_with_db.delete("/api/v1/auth/me/phone/override")

        assert response.status_code == 200
        data = response.json()
        assert "phone_number" in data
        assert "phone_number_idp" in data
        assert "phone_number_override" in data
        assert data["phone_number_override"] is None


class TestPatchSettingsPhoneGuard:
    """Tests for the PATCH /me/settings guard that blocks direct phone_number_override."""

    @pytest.mark.asyncio
    async def test_patch_phone_override_blocked_when_verify_configured(self, client_with_db):
        """Setting a non-null phone_number_override via PATCH is rejected when Verify is configured."""
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True

            response = await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"phone_number_override": "+41791234567"},
            )

        assert response.status_code == 400
        assert "verified" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_patch_phone_override_clear_allowed(self, client_with_db):
        """Clearing phone_number_override (null) via PATCH is always allowed."""
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True

            response = await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"phone_number_override": None},
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_phone_override_allowed_when_verify_not_configured(self, client_with_db):
        """Setting phone_number_override via PATCH is allowed when Verify is not configured."""
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = False

            response = await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"phone_number_override": "+41791234567"},
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_other_fields_unaffected_by_guard(self, client_with_db):
        """PATCH with non-phone fields works regardless of Verify config."""
        with patch("console_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = True

            response = await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"language": "de"},
            )

        assert response.status_code == 200
