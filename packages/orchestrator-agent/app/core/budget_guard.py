"""Budget guard for monthly LLM spend monitoring and enforcement.

Fail-closed budget enforcement. The budget itself — monthly USD limit, warning
thresholds, enabled flag — lives in console-backend's admin-editable `budget_settings`
and is evaluated against the gateway usage logs (the spend source of truth). This guard
polls ``GET /api/v1/admin/budget/status`` on an interval, caches the returned lock
decision, and the executor rejects new work while locked.

Auth: the poll runs in a background loop with no user request context, so it uses the
orchestrator's OIDC client-credentials token (azp = orchestrator), which console-backend
accepts on that endpoint via ``require_admin_or_orchestrator`` — same pattern as the
risk-score API client.

Fail-closed: if the status endpoint is unreachable or returns an error, the guard locks
to prevent runaway spend while the budget can't be verified.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from ringier_a2a_sdk.oauth import OidcOAuth2Client

logger = logging.getLogger(__name__)

_STATUS_PATH = "/api/v1/admin/budget/status"
_HTTP_TIMEOUT = 10.0

# Module-level singleton instance
_budget_guard: Optional["BudgetGuard"] = None


def get_budget_guard() -> Optional["BudgetGuard"]:
    """Get the singleton BudgetGuard instance (None if not initialized)."""
    return _budget_guard


def init_budget_guard(
    base_url: str,
    oauth2_client: "OidcOAuth2Client | None" = None,
    audience: str = "agent-console",
    check_interval_seconds: int = 300,
) -> "BudgetGuard":
    """Initialize the singleton BudgetGuard. Call once during application startup.

    There is no local enable/disable knob: enforcement is turned on/off from the console
    (budget_settings.enabled), which the status endpoint reflects in `is_locked`. The guard
    only declines to poll when it has no way to authenticate (no OIDC client, e.g. local
    dev) — a capability check, not a config toggle.

    Args:
        base_url: console-backend base URL (CONSOLE_BACKEND_URL).
        oauth2_client: OIDC client for the client-credentials service token. When None
            (local dev with no OIDC), the guard stays inert rather than failing closed.
        audience: console-backend's client id (token audience).
        check_interval_seconds: Poll interval (default 300 = 5 minutes).
    """
    global _budget_guard
    _budget_guard = BudgetGuard(
        base_url=base_url,
        oauth2_client=oauth2_client,
        audience=audience,
        check_interval_seconds=check_interval_seconds,
    )
    return _budget_guard


@dataclass
class BudgetStatus:
    """Current budget status snapshot (mirrors console-backend's BudgetStatus)."""

    spend_usd: Decimal
    limit_usd: Decimal
    usage_percentage: float
    is_locked: bool
    last_refresh: Optional[datetime]
    warnings: list[float]


@dataclass
class BudgetGuard:
    """Polls console-backend for the budget lock decision and enforces it.

    Enforcement on/off lives server-side (budget_settings.enabled) and is surfaced in the
    polled `is_locked`; this guard has no local toggle. It only stays inert when it can't
    authenticate (no OIDC client — local dev), which `enabled` reports as a capability.

    Fail-closed: when polling fails the guard locks until a healthy status is seen again.

    Attributes:
        base_url: console-backend base URL.
        oauth2_client: OIDC client-credentials client (None keeps the guard inert).
        audience: console-backend client id used as the token audience.
        check_interval_seconds: Poll interval (default 300 = 5 minutes).
    """

    base_url: str
    oauth2_client: "OidcOAuth2Client | None" = None
    audience: str = "agent-console"
    check_interval_seconds: int = 300

    # Internal state
    _is_locked: bool = field(default=False, init=False)
    _lock_reason: Optional[str] = field(default=None, init=False)
    _spend_usd: Decimal = field(default_factory=lambda: Decimal("0"), init=False)
    _limit_usd: Decimal = field(default_factory=lambda: Decimal("0"), init=False)
    _usage_percentage: float = field(default=0.0, init=False)
    _warnings: list[float] = field(default_factory=list, init=False)
    _last_refresh: Optional[datetime] = field(default=None, init=False)
    _client: Optional[httpx.AsyncClient] = field(default=None, init=False, repr=False)
    _polling_task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/") if self.base_url else ""
        if self.enabled:
            logger.info("BudgetGuard initialized (polling %s every %ss)", self.base_url, self.check_interval_seconds)
        else:
            logger.info("BudgetGuard inert: no OIDC client (local dev) — no polling or enforcement")

    @property
    def enabled(self) -> bool:
        """Whether the guard can operate (authenticate + poll).

        This is a capability flag, NOT the enforcement on/off switch — that lives in the
        console (budget_settings.enabled) and is reflected by the polled `is_locked`. The
        guard is inert in environments with no OIDC client (local dev).
        """
        return self.oauth2_client is not None and bool(self.base_url)

    @property
    def is_locked(self) -> bool:
        """True if budget is locked (exceeded or status unavailable) and the guard can operate."""
        return self._is_locked and self.enabled

    def get_status(self) -> BudgetStatus:
        """Get the last-polled budget status snapshot."""
        return BudgetStatus(
            spend_usd=self._spend_usd,
            limit_usd=self._limit_usd,
            usage_percentage=self._usage_percentage,
            is_locked=self._is_locked,
            last_refresh=self._last_refresh,
            warnings=list(self._warnings),
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=_HTTP_TIMEOUT)
        return self._client

    def _lock(self, reason: str) -> None:
        if not self._is_locked:
            logger.warning("BudgetGuard LOCKED: %s", reason)
        self._is_locked = True
        self._lock_reason = reason

    def _unlock(self) -> None:
        if self._is_locked:
            logger.info("BudgetGuard unlocked")
        self._is_locked = False
        self._lock_reason = None

    async def refresh(self) -> None:
        """Poll console-backend for the budget status and update the cached lock decision.

        Fail-closed: on any error (no token, network, non-200) the guard locks so we don't
        keep serving traffic while spend can't be verified.
        """
        if not self.enabled:
            return

        try:
            token = await self.oauth2_client.get_token(self.audience)  # type: ignore[union-attr]
        except Exception as e:
            self._lock(f"Could not obtain service token: {e}")
            return

        try:
            resp = await self._get_client().get(
                _STATUS_PATH, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code != 200:
                self._lock(f"Budget status endpoint returned {resp.status_code}")
                return

            data = resp.json()
            self._spend_usd = Decimal(str(data.get("spend_usd", "0")))
            self._limit_usd = Decimal(str(data.get("limit_usd", "0")))
            self._usage_percentage = float(data.get("usage_percentage", 0.0))
            self._warnings = list(data.get("warnings", []))
            self._last_refresh = datetime.now(timezone.utc)

            if data.get("is_locked"):
                self._lock("Monthly spending budget exceeded")
            else:
                self._unlock()

            logger.debug(
                "Budget refresh: $%s / $%s (%.1f%%) locked=%s",
                self._spend_usd, self._limit_usd, self._usage_percentage, self._is_locked,
            )
        except Exception as e:
            # FAIL-CLOSED: lock on any error.
            self._lock(f"Budget status poll failed: {e}")

    async def start_polling(self) -> None:
        """Start the background polling task."""
        if not self.enabled:
            logger.info("BudgetGuard disabled, skipping polling")
            return
        if self._polling_task is not None:
            logger.warning("Polling task already running")
            return
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("BudgetGuard polling started (interval: %ss)", self.check_interval_seconds)

    async def stop_polling(self) -> None:
        """Stop the background polling task gracefully."""
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
            logger.info("BudgetGuard polling stopped")
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _polling_loop(self) -> None:
        """Background loop that periodically refreshes the budget status."""
        # Initial refresh immediately.
        await self.refresh()

        while True:
            try:
                await asyncio.sleep(self.check_interval_seconds)
                await self.refresh()
            except asyncio.CancelledError:
                logger.debug("Polling loop cancelled")
                raise
            except Exception as e:
                logger.error("Unexpected error in budget polling loop: %s", e)
                await asyncio.sleep(self.check_interval_seconds)
