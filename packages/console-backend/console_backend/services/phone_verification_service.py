"""Phone number verification service using Twilio Verify."""

import logging
import re

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from ..config import config

logger = logging.getLogger(__name__)

# E.164 format: + followed by 1-15 digits
E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")


class PhoneVerificationService:
    """Handles phone number verification via Twilio Verify API."""

    def __init__(self) -> None:
        self._client: Client | None = None

    @staticmethod
    def _mask_phone_number(phone_number: str) -> str:
        """Mask phone number for safe logging."""
        if not phone_number:
            return "***"
        visible_digits = 4
        if len(phone_number) <= visible_digits:
            return "*" * len(phone_number)
        return f"{'*' * (len(phone_number) - visible_digits)}{phone_number[-visible_digits:]}"

    @property
    def client(self) -> Client:
        """Lazy-init Twilio client.

        Verify is only available in the default US region — do NOT pass
        region/edge here.
        """
        if self._client is None:
            cfg = config.twilio_verify
            self._client = Client(cfg.api_key, cfg.api_secret.get_secret_value(), cfg.account_sid)
        return self._client

    @property
    def is_configured(self) -> bool:
        """Check if Twilio Verify is available."""
        return config.twilio_verify.is_configured

    @staticmethod
    def validate_e164(phone_number: str) -> bool:
        """Validate E.164 phone number format."""
        return bool(E164_PATTERN.match(phone_number))

    async def send_verification(self, phone_number: str, channel: str = "sms") -> bool:
        """Send a verification code to the phone number.

        Args:
            phone_number: E.164 formatted phone number.
            channel: Delivery channel — "sms" or "call".

        Returns:
            True if the verification was sent successfully.

        Raises:
            ValueError: If phone number format is invalid or channel is unsupported.
            RuntimeError: If Twilio Verify is not configured.
        """
        if not self.is_configured:
            raise RuntimeError("Twilio Verify is not configured")

        if not self.validate_e164(phone_number):
            raise ValueError(f"Invalid E.164 phone number: {phone_number}")

        if channel not in ("sms", "call"):
            raise ValueError(f"Unsupported verification channel: {channel}. Use 'sms' or 'call'.")

        try:
            verification = self.client.verify.v2.services(config.twilio_verify.verify_service_sid).verifications.create(
                to=phone_number, channel=channel
            )
            logger.debug(
                "Verification sent to %s via %s (status=%s)",
                self._mask_phone_number(phone_number),
                channel,
                verification.status,
            )
            return verification.status == "pending"
        except TwilioRestException as e:
            logger.error(f"Twilio Verify send failed: {e}")
            raise

    async def check_verification(self, phone_number: str, code: str) -> bool:
        """Check a verification code.

        Args:
            phone_number: E.164 formatted phone number.
            code: The verification code entered by the user.

        Returns:
            True if the code is correct and verification is approved.
        """
        if not self.is_configured:
            raise RuntimeError("Twilio Verify is not configured")

        try:
            check = self.client.verify.v2.services(config.twilio_verify.verify_service_sid).verification_checks.create(
                to=phone_number, code=code
            )
            logger.debug(
                "Verification check for %s: status=%s",
                self._mask_phone_number(phone_number),
                check.status,
            )
            return check.status == "approved"
        except TwilioRestException as e:
            logger.error(f"Twilio Verify check failed: {e}")
            return False
