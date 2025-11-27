"""Budget guard for Bedrock token usage monitoring and enforcement.

This module implements a fail-closed budget enforcement system that polls
LangSmith every 5 minutes to track monthly token usage and lock Bedrock
calls when the budget is exceeded.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from langsmith import Client

logger = logging.getLogger(__name__)

# Module-level singleton instance
_budget_guard: Optional["BudgetGuard"] = None


def get_budget_guard() -> Optional["BudgetGuard"]:
    """Get the singleton BudgetGuard instance.

    Returns:
        The BudgetGuard instance if initialized, None otherwise.
    """
    return _budget_guard


def init_budget_guard(
    project_name: str,
    token_limit: int,
    check_interval_seconds: int = 300,
    warning_thresholds: tuple[float, ...] = (0.80, 0.90, 0.95),
    enabled: bool = True,
) -> "BudgetGuard":
    """Initialize the singleton BudgetGuard instance.

    Should be called once during application startup (in lifespan).

    Args:
        project_name: LangSmith project name to monitor
        token_limit: Maximum tokens allowed per month
        check_interval_seconds: Polling interval (default: 300 = 5 minutes)
        warning_thresholds: Percentage thresholds for warnings
        enabled: Whether budget enforcement is active

    Returns:
        The initialized BudgetGuard instance.
    """
    global _budget_guard
    _budget_guard = BudgetGuard(
        project_name=project_name,
        token_limit=token_limit,
        check_interval_seconds=check_interval_seconds,
        warning_thresholds=warning_thresholds,
        enabled=enabled,
    )
    return _budget_guard


@dataclass
class BudgetStatus:
    """Current budget status snapshot."""

    current_usage: int
    token_limit: int
    usage_percentage: float
    is_locked: bool
    last_refresh: Optional[datetime]
    lock_reason: Optional[str]
    warnings_sent: list[float]


@dataclass
class BudgetGuard:
    """Monitors and enforces token budget limits via LangSmith polling.

    Implements a fail-closed design: if LangSmith API fails, the guard
    locks to prevent runaway costs.

    Attributes:
        project_name: LangSmith project name to monitor
        token_limit: Maximum tokens allowed per month (mutable at runtime)
        check_interval_seconds: Polling interval (default: 300 = 5 minutes)
        warning_thresholds: Percentage thresholds for warnings (default: 80%, 90%, 95%)
        enabled: Whether budget enforcement is active
    """

    project_name: str
    token_limit: int
    check_interval_seconds: int = 300
    warning_thresholds: tuple[float, ...] = (0.80, 0.90, 0.95)
    enabled: bool = True

    # Internal state
    _client: Optional[Client] = field(default=None, repr=False)
    _is_locked: bool = field(default=False, init=False)
    _lock_reason: Optional[str] = field(default=None, init=False)
    _current_usage: int = field(default=0, init=False)
    _last_refresh: Optional[datetime] = field(default=None, init=False)
    _warnings_sent: set[float] = field(default_factory=set, init=False)
    _last_warning_month: Optional[int] = field(default=None, init=False)
    _polling_task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize LangSmith client."""
        if self.enabled:
            try:
                self._client = Client()
                logger.info(
                    f"BudgetGuard initialized for project '{self.project_name}' with limit {self.token_limit:,} tokens"
                )
            except Exception as e:
                logger.error(f"Failed to initialize LangSmith client: {e}")
                self._lock(f"Failed to initialize LangSmith client: {e}")

    @property
    def is_locked(self) -> bool:
        """Check if budget is locked (either exceeded or API error)."""
        return self._is_locked and self.enabled

    def get_status(self) -> BudgetStatus:
        """Get current budget status snapshot."""
        usage_pct = (self._current_usage / self.token_limit * 100) if self.token_limit > 0 else 0.0
        return BudgetStatus(
            current_usage=self._current_usage,
            token_limit=self.token_limit,
            usage_percentage=round(usage_pct, 2),
            is_locked=self._is_locked,
            last_refresh=self._last_refresh,
            lock_reason=self._lock_reason,
            warnings_sent=sorted(self._warnings_sent),
        )

    def set_token_limit(self, new_limit: int) -> None:
        """Update token limit at runtime.

        Args:
            new_limit: New monthly token limit

        Note:
            This change is temporary and will reset on restart.
            If new limit is higher than current usage, unlock.
        """
        old_limit = self.token_limit
        self.token_limit = new_limit
        logger.info(f"Budget token limit updated: {old_limit:,} -> {new_limit:,}")

        # Re-evaluate lock status
        if self._current_usage < new_limit and self._lock_reason == "Budget exceeded":
            self._unlock()
            logger.info("Budget unlocked after limit increase")

    def _lock(self, reason: str) -> None:
        """Lock the budget guard."""
        if not self._is_locked:
            self._is_locked = True
            self._lock_reason = reason
            logger.warning(f"BudgetGuard LOCKED: {reason}")

    def _unlock(self) -> None:
        """Unlock the budget guard."""
        self._is_locked = False
        self._lock_reason = None
        logger.info("BudgetGuard unlocked")

    def _get_month_start(self) -> datetime:
        """Get the start of the current month in UTC."""
        now = datetime.now(timezone.utc)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _reset_warnings_if_new_month(self) -> None:
        """Reset warnings if we've entered a new month."""
        current_month = datetime.now(timezone.utc).month
        if self._last_warning_month is not None and self._last_warning_month != current_month:
            logger.info(f"New month detected, resetting warnings (was month {self._last_warning_month})")
            self._warnings_sent.clear()
            # Also unlock if previously locked due to budget
            if self._lock_reason == "Budget exceeded":
                self._unlock()
        self._last_warning_month = current_month

    async def refresh(self) -> None:
        """Refresh token usage from LangSmith.

        This method is safe to call concurrently - it uses asyncio.to_thread
        to avoid blocking the event loop.

        Fail-closed: On any API error, the guard locks to prevent runaway costs.
        """
        if not self.enabled or not self._client:
            return

        self._reset_warnings_if_new_month()

        try:
            # LangSmith client is sync, wrap in thread
            stats = await asyncio.to_thread(
                self._client.get_run_stats,
                project_names=[self.project_name],
                start_time=self._get_month_start().isoformat(),
            )

            # Extract total tokens from stats
            # Stats structure: {"total_tokens": int, "prompt_tokens": int, "completion_tokens": int, ...}
            total_tokens = stats.get("total_tokens", 0) or 0
            self._current_usage = total_tokens
            self._last_refresh = datetime.now(timezone.utc)

            logger.debug(
                f"Budget refresh: {total_tokens:,} / {self.token_limit:,} tokens "
                f"({total_tokens / self.token_limit * 100:.1f}%)"
            )

            # Check thresholds and emit warnings
            usage_ratio = total_tokens / self.token_limit if self.token_limit > 0 else 0

            for threshold in self.warning_thresholds:
                if usage_ratio >= threshold and threshold not in self._warnings_sent:
                    self._warnings_sent.add(threshold)
                    logger.warning(
                        f"BUDGET WARNING: {threshold * 100:.0f}% threshold reached! "
                        f"Usage: {total_tokens:,} / {self.token_limit:,} tokens"
                    )

            # Lock if budget exceeded
            if usage_ratio >= 1.0:
                self._lock("Budget exceeded")
            elif self._lock_reason == "Budget exceeded":
                # Budget was exceeded but now we're under (limit was increased)
                self._unlock()

        except Exception as e:
            # FAIL-CLOSED: Lock on any API error
            logger.error(f"LangSmith API error during budget refresh: {e}")
            if "not found" in str(e).lower():
                logger.info(f"Project '{self.project_name}' not found in LangSmith, allowing calls")
                if self._lock_reason == "Budget exceeded":
                    self._unlock()
            else:
                self._lock(f"LangSmith API error: {e}")

    async def start_polling(self) -> None:
        """Start the background polling task."""
        if not self.enabled:
            logger.info("BudgetGuard disabled, skipping polling")
            return

        if self._polling_task is not None:
            logger.warning("Polling task already running")
            return

        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info(f"BudgetGuard polling started (interval: {self.check_interval_seconds}s)")

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

    async def _polling_loop(self) -> None:
        """Background loop that periodically refreshes token usage."""
        # Initial refresh immediately
        await self.refresh()

        while True:
            try:
                await asyncio.sleep(self.check_interval_seconds)
                await self.refresh()
            except asyncio.CancelledError:
                logger.debug("Polling loop cancelled")
                raise
            except Exception as e:
                # Log but don't crash the loop
                logger.error(f"Unexpected error in polling loop: {e}")
                # Still sleep to avoid tight error loop
                await asyncio.sleep(self.check_interval_seconds)
