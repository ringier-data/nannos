"""Tests for time_tools module."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.time_tools import (
    _apply_delta,
    _format_datetime,
    _get_base_datetime,
    create_time_tool,
)


class TestGetBaseDatetime:
    """Test _get_base_datetime function."""

    def test_base_now(self):
        """Test 'now' returns current datetime."""
        tz = ZoneInfo("Europe/Zurich")
        result = _get_base_datetime("now", tz)

        # Should be close to current time
        now = datetime.now(tz)
        assert abs((now - result).total_seconds()) < 1

    def test_base_today(self):
        """Test 'today' returns midnight of current day."""
        tz = ZoneInfo("Europe/Zurich")
        result = _get_base_datetime("today", tz)

        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0
        assert result.microsecond == 0

    def test_base_start_of_week(self):
        """Test 'start_of_week' returns Monday at midnight."""
        tz = ZoneInfo("Europe/Zurich")
        result = _get_base_datetime("start_of_week", tz)

        assert result.weekday() == 0  # Monday
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0

    def test_base_start_of_month(self):
        """Test 'start_of_month' returns first day at midnight."""
        tz = ZoneInfo("Europe/Zurich")
        result = _get_base_datetime("start_of_month", tz)

        assert result.day == 1
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0

    def test_base_start_of_year(self):
        """Test 'start_of_year' returns January 1st at midnight."""
        tz = ZoneInfo("Europe/Zurich")
        result = _get_base_datetime("start_of_year", tz)

        assert result.month == 1
        assert result.day == 1
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0


class TestApplyDelta:
    """Test _apply_delta function."""

    def test_no_delta(self):
        """Test that None delta returns unchanged datetime."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, None, None)
        assert result == dt

    def test_add_minutes(self):
        """Test adding minutes."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 30, "minutes")
        assert result.hour == 12
        assert result.minute == 30

    def test_add_hours(self):
        """Test adding hours."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 3, "hours")
        assert result.hour == 15

    def test_add_days(self):
        """Test adding days."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 1, "days")
        assert result.day == 10

    def test_add_weeks(self):
        """Test adding weeks."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 1, "weeks")
        assert result.day == 16

    def test_add_months(self):
        """Test adding months with relativedelta."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 1, "months")
        assert result.month == 1
        assert result.year == 2026
        assert result.day == 9  # Precise month arithmetic

    def test_subtract_days(self):
        """Test subtracting days with negative delta."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, -7, "days")
        assert result.day == 2

    def test_subtract_months(self):
        """Test subtracting months."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, -1, "months")
        assert result.month == 11
        assert result.year == 2025


class TestFormatDatetime:
    """Test _format_datetime function."""

    def test_format_iso8601(self):
        """Test ISO 8601 format."""
        dt = datetime(2025, 12, 9, 15, 30, 45, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "iso8601", ZoneInfo("UTC"))
        assert "2025-12-09" in result
        assert "15:30:45" in result

    def test_format_unix(self):
        """Test Unix timestamp format."""
        dt = datetime(2025, 12, 9, 15, 30, 0, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "unix", ZoneInfo("UTC"))
        assert result.isdigit()
        assert int(result) > 1700000000  # After 2023

    def test_format_human(self):
        """Test human-readable format."""
        dt = datetime(2025, 12, 9, 15, 30, 0, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "human", ZoneInfo("UTC"))
        assert "December" in result
        assert "2025" in result

    def test_format_date_only(self):
        """Test date-only format."""
        dt = datetime(2025, 12, 9, 15, 30, 0, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "date_only", ZoneInfo("UTC"))
        assert result == "2025-12-09"

    def test_format_time_only(self):
        """Test time-only format."""
        dt = datetime(2025, 12, 9, 15, 30, 45, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "time_only", ZoneInfo("UTC"))
        assert result == "15:30:45"

    def test_format_with_timezone_conversion(self):
        """Test formatting with timezone conversion."""
        dt = datetime(2025, 12, 9, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _format_datetime(dt, "iso8601", ZoneInfo("America/New_York"))
        # UTC 12:00 should be 07:00 in New York (EST, UTC-5)
        assert "07:00" in result or "06:00" in result or "07:" in result  # Account for DST


class TestTimeToolCreation:
    """Test time tool creation and execution."""

    def test_create_time_tool(self):
        """Test that create_time_tool returns a valid tool."""
        tool = create_time_tool()

        assert tool is not None
        assert tool.name == "get_current_time"
        assert "time" in tool.description.lower()

    def test_tool_invoke_now(self):
        """Test invoking tool with 'now' base."""
        tool = create_time_tool()
        result = tool.invoke({"base": "now", "format": "iso8601", "timezone": "UTC"})

        assert isinstance(result, str)
        assert "2026" in result  # Current year

    def test_tool_invoke_today(self):
        """Test invoking tool with 'today' base."""
        tool = create_time_tool()
        result = tool.invoke({"base": "today", "format": "date_only", "timezone": "UTC"})

        assert isinstance(result, str)
        assert len(result) == 10  # YYYY-MM-DD format
        assert "2026" in result

    def test_tool_invoke_tomorrow(self):
        """Test calculating tomorrow."""
        tool = create_time_tool()
        result = tool.invoke(
            {"base": "today", "delta_value": 1, "delta_unit": "days", "format": "date_only", "timezone": "UTC"}
        )

        assert isinstance(result, str)
        assert "2026" in result

    def test_tool_invoke_next_week(self):
        """Test calculating next week."""
        tool = create_time_tool()
        result = tool.invoke(
            {"base": "start_of_week", "delta_value": 1, "delta_unit": "weeks", "format": "date_only", "timezone": "UTC"}
        )

        assert isinstance(result, str)
        assert "2025" in result or "2026" in result

    def test_tool_invoke_invalid_timezone(self):
        """Test handling of invalid timezone."""
        tool = create_time_tool()
        result = tool.invoke({"base": "now", "format": "iso8601", "timezone": "Invalid/Timezone"})

        assert isinstance(result, str)
        assert "Warning" in result or "UTC" in result

    def test_tool_invoke_with_timezone(self):
        """Test tool respects timezone parameter."""
        tool = create_time_tool()
        result = tool.invoke({"base": "now", "format": "iso8601", "timezone": "America/New_York"})

        assert isinstance(result, str)
        # Should contain timezone offset
        assert "-" in result or "+" in result

    def test_tool_invoke_unix_format(self):
        """Test Unix timestamp output."""
        tool = create_time_tool()
        result = tool.invoke({"base": "now", "format": "unix", "timezone": "UTC"})

        assert isinstance(result, str)
        assert result.isdigit()
        timestamp = int(result)
        assert 1700000000 < timestamp < 2000000000  # Reasonable range

    def test_tool_defaults(self):
        """Test tool works with minimal parameters (defaults)."""
        tool = create_time_tool()
        result = tool.invoke({})

        assert isinstance(result, str)
        # Should use default timezone (Europe/Zurich) and format (iso8601)
        assert "2026" in result


class TestMonthArithmetic:
    """Test precise month arithmetic with relativedelta."""

    def test_add_month_to_january_31(self):
        """Test adding 1 month to January 31 handles February correctly."""
        dt = datetime(2025, 1, 31, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 1, "months")
        # Should handle February's shorter length gracefully
        assert result.month == 2
        assert result.year == 2025

    def test_add_month_end_of_year(self):
        """Test adding month crosses year boundary."""
        dt = datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, 1, "months")
        assert result.month == 1
        assert result.year == 2026
        assert result.day == 15

    def test_subtract_months_crosses_year(self):
        """Test subtracting months crosses year boundary."""
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        result = _apply_delta(dt, -2, "months")
        assert result.month == 11
        assert result.year == 2024
        assert result.day == 15


class TestDSTHandling:
    """Test DST (Daylight Saving Time) handling."""

    def test_timezone_aware_calculation(self):
        """Test that calculations are timezone-aware."""
        tz = ZoneInfo("America/New_York")
        dt = datetime(2025, 3, 9, 12, 0, 0, tzinfo=tz)  # Near DST transition
        result = _apply_delta(dt, 1, "days")

        # Should maintain local time correctly despite DST
        assert result.hour == 12
        assert result.day == 10
