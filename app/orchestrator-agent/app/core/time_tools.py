"""Time handling tools for the orchestrator agent.

These tools allow the LLM to:
1. Get current time/date with timezone awareness
2. Calculate relative dates using structured parameters (Unix-like pattern)
3. Format datetime outputs in various formats

The tool uses structured enums to ensure reliable, predictable behavior
and follows Unix `date` command conventions familiar from LLM training data.

Examples:
    # Get current time in user's timezone
    get_current_time(base="now", timezone="America/New_York")

    # Get tomorrow's date
    get_current_time(base="today", delta_value=1, delta_unit="days")

    # Get start of next week
    get_current_time(base="start_of_week", delta_value=1, delta_unit="weeks")
"""

import logging
from datetime import datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Type aliases for enum values
BaseTime = Literal["now", "today", "start_of_week", "start_of_month", "start_of_year"]
DeltaUnit = Literal["minutes", "hours", "days", "weeks", "months"]
OutputFormat = Literal["iso8601", "unix", "human", "date_only", "time_only"]


class GetCurrentTimeInput(BaseModel):
    """Input schema for get_current_time tool.

    Uses structured enums to ensure predictable, reliable behavior.
    Follows Unix `date` command patterns familiar from LLM training data.
    """

    base: BaseTime = Field(
        default="now",
        description=(
            "Base time reference point:\n"
            "- 'now': Current datetime with time component\n"
            "- 'today': Today at 00:00:00 (midnight)\n"
            "- 'start_of_week': Start of current week (Monday 00:00:00)\n"
            "- 'start_of_month': Start of current month (1st at 00:00:00)\n"
            "- 'start_of_year': Start of current year (Jan 1st at 00:00:00)"
        ),
    )

    delta_value: Optional[int] = Field(
        default=None,
        description=(
            "Optional offset amount (can be positive or negative). "
            "Use with delta_unit to calculate relative times. "
            "Examples: +1 for tomorrow, -7 for last week, +2 for next month"
        ),
    )

    delta_unit: Optional[DeltaUnit] = Field(
        default=None,
        description=(
            "Time unit for delta_value:\n"
            "- 'minutes': Minute-level precision\n"
            "- 'hours': Hour-level precision\n"
            "- 'days': Day-level precision\n"
            "- 'weeks': Week-level precision (7 days)\n"
            "- 'months': Month-level precision (approximate, 30 days)"
        ),
    )

    format: OutputFormat = Field(
        default="iso8601",
        description=(
            "Output format:\n"
            "- 'iso8601': ISO 8601 format with timezone (2025-12-09T15:30:00+01:00)\n"
            "- 'unix': Unix timestamp in seconds (1733753400)\n"
            "- 'human': Human-readable format (Monday, December 9, 2025 3:30 PM CET)\n"
            "- 'date_only': Date only (2025-12-09)\n"
            "- 'time_only': Time only (15:30:00)"
        ),
    )

    timezone: str = Field(
        default="Europe/Zurich",
        description=(
            "IANA timezone name (e.g., 'America/New_York', 'Europe/Berlin', 'Asia/Tokyo'). "
            "Defaults to 'Europe/Zurich' if not provided or invalid. "
            "Use user's configured timezone when available from context."
        ),
    )


def _get_base_datetime(base: BaseTime, tz: ZoneInfo) -> datetime:
    """Calculate base datetime based on the base parameter.

    Args:
        base: Base time reference point
        tz: Timezone for calculations

    Returns:
        Datetime object at the base reference point
    """
    now = datetime.now(tz)

    if base == "now":
        return now
    elif base == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif base == "start_of_week":
        # Monday is 0, Sunday is 6
        days_since_monday = now.weekday()
        start_of_week = now - relativedelta(days=days_since_monday)
        return start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    elif base == "start_of_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif base == "start_of_year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        # Fallback to now
        return now


def _apply_delta(dt: datetime, delta_value: Optional[int], delta_unit: Optional[DeltaUnit]) -> datetime:
    """Apply time delta to a datetime object.

    Uses relativedelta for consistent handling of calendar arithmetic,
    including proper handling of DST transitions and month boundaries.

    Args:
        dt: Base datetime
        delta_value: Amount to offset
        delta_unit: Unit for the offset

    Returns:
        Datetime with delta applied
    """
    if delta_value is None or delta_unit is None:
        return dt

    if delta_unit == "minutes":
        return dt + relativedelta(minutes=delta_value)
    elif delta_unit == "hours":
        return dt + relativedelta(hours=delta_value)
    elif delta_unit == "days":
        return dt + relativedelta(days=delta_value)
    elif delta_unit == "weeks":
        return dt + relativedelta(weeks=delta_value)
    elif delta_unit == "months":
        return dt + relativedelta(months=delta_value)
    else:
        return dt


def _format_datetime(dt: datetime, format: OutputFormat, tz: ZoneInfo) -> str:
    """Format datetime according to the specified format.

    Args:
        dt: Datetime to format
        format: Desired output format
        tz: Timezone for formatting

    Returns:
        Formatted datetime string
    """
    # Ensure datetime is in the correct timezone
    dt_in_tz = dt.astimezone(tz)

    if format == "iso8601":
        return dt_in_tz.isoformat()
    elif format == "unix":
        return str(int(dt_in_tz.timestamp()))
    elif format == "human":
        # Format: Monday, December 9, 2025 3:30 PM CET
        return dt_in_tz.strftime("%A, %B %d, %Y %I:%M %p %Z")
    elif format == "date_only":
        return dt_in_tz.strftime("%Y-%m-%d")
    elif format == "time_only":
        return dt_in_tz.strftime("%H:%M:%S")
    else:
        # Fallback to ISO 8601
        return dt_in_tz.isoformat()


def _create_get_current_time_tool() -> BaseTool:
    """Create tool for getting current time with timezone awareness and relative date calculations.

    Returns:
        StructuredTool for time operations
    """

    def get_current_time_handler(
        base: BaseTime = "now",
        delta_value: Optional[int] = None,
        delta_unit: Optional[DeltaUnit] = None,
        format: OutputFormat = "iso8601",
        timezone: str = "Europe/Zurich",
    ) -> str:
        """Get current time or calculate relative dates with timezone awareness.

        Use this tool when you need to:
        - Get the current date/time in the user's timezone
        - Calculate relative dates (tomorrow, next week, last month, etc.)
        - Convert between different time formats
        - Answer time-related queries from users

        IMPORTANT: ALWAYS use this tool for time-related queries instead of relying
        on your training data, which may be outdated. The tool provides the ACTUAL
        current time.

        Examples:
            # Current time in user's timezone
            get_current_time(base="now", timezone="Europe/Berlin")

            # Tomorrow at midnight
            get_current_time(base="today", delta_value=1, delta_unit="days")

            # Next Monday (start of next week)
            get_current_time(base="start_of_week", delta_value=1, delta_unit="weeks")

            # Two hours from now
            get_current_time(base="now", delta_value=2, delta_unit="hours")

            # Start of last month
            get_current_time(base="start_of_month", delta_value=-1, delta_unit="months")

        Args:
            base: Base time reference ('now', 'today', 'start_of_week', 'start_of_month', 'start_of_year')
            delta_value: Optional offset amount (positive or negative integer)
            delta_unit: Time unit for delta ('minutes', 'hours', 'days', 'weeks', 'months')
            format: Output format ('iso8601', 'unix', 'human', 'date_only', 'time_only')
            timezone: IANA timezone name (defaults to 'UTC')

        Returns:
            Formatted datetime string, or error message if timezone is invalid
        """
        try:
            # Validate and load timezone
            try:
                tz = ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                logger.warning(f"Invalid timezone '{timezone}', falling back to UTC")
                tz = ZoneInfo("UTC")
                return (
                    f"Warning: Invalid timezone '{timezone}'. Using UTC instead.\n"
                    f"Please use a valid IANA timezone name (e.g., 'America/New_York', 'Europe/Berlin').\n\n"
                    f"Current time in UTC: {_format_datetime(datetime.now(tz), format, tz)}"
                )

            # Calculate base datetime
            dt = _get_base_datetime(base, tz)  # type: ignore[arg-type]

            # Apply delta if provided
            dt = _apply_delta(dt, delta_value, delta_unit)  # type: ignore[arg-type]

            # Format output
            formatted = _format_datetime(dt, format, tz)  # type: ignore[arg-type]

            logger.info(
                f"Time calculation: base={base}, delta={delta_value} {delta_unit}, "
                f"tz={timezone}, format={format} -> {formatted}"
            )

            return formatted

        except Exception as e:
            error_msg = f"Error calculating time: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return error_msg

    return StructuredTool.from_function(
        func=get_current_time_handler,
        name="get_current_time",
        description=(
            "Get current time or calculate relative dates with timezone awareness. "
            "Use structured parameters (base, delta_value, delta_unit) to calculate "
            "times like 'tomorrow', 'next week', 'last month', etc. "
            "CRITICAL: ALWAYS use this tool for time queries instead of using your "
            "training data - this provides the ACTUAL current time. "
            "Examples: tomorrow = base='today' + delta_value=1 + delta_unit='days', "
            "next week = base='start_of_week' + delta_value=1 + delta_unit='weeks'."
        ),
        args_schema=GetCurrentTimeInput,
    )


def create_time_tool() -> BaseTool:
    """Create the time tool for external use.

    Returns:
        Tool for time operations
    """
    return _create_get_current_time_tool()
