"""Unit tests for cookie utilities."""

from datetime import datetime, timezone

import pytest

from playground_backend.utils.cookie_utils import (
    CookieAttributes,
    extract_cookie_attributes,
    find_cookie_in_headers,
    parse_set_cookie,
    validate_cookie_domain,
)


class TestParseSetCookie:
    """Tests for parse_set_cookie function."""

    def test_parse_simple_cookie(self) -> None:
        """Test parsing a simple cookie without attributes."""
        cookie = parse_set_cookie("session=abc123")
        assert "session" in cookie
        assert cookie["session"].value == "abc123"

    def test_parse_cookie_with_max_age(self) -> None:
        """Test parsing a cookie with Max-Age attribute."""
        cookie = parse_set_cookie("session=abc123; Max-Age=3600")
        assert cookie["session"].value == "abc123"
        assert cookie["session"]["max-age"] == "3600"

    def test_parse_cookie_with_domain(self) -> None:
        """Test parsing a cookie with Domain attribute."""
        cookie = parse_set_cookie("session=abc123; Domain=.example.com")
        assert cookie["session"]["domain"] == ".example.com"

    def test_parse_cookie_with_all_attributes(self) -> None:
        """Test parsing a cookie with all common attributes."""
        cookie = parse_set_cookie(
            "session=abc123; Max-Age=3600; Domain=.example.com; Path=/; Secure; HttpOnly; SameSite=Strict"
        )
        assert cookie["session"].value == "abc123"
        assert cookie["session"]["max-age"] == "3600"
        assert cookie["session"]["domain"] == ".example.com"
        assert cookie["session"]["path"] == "/"
        assert cookie["session"]["secure"] is True
        assert cookie["session"]["httponly"] is True
        assert cookie["session"]["samesite"] == "Strict"


class TestExtractCookieAttributes:
    """Tests for extract_cookie_attributes function."""

    def test_extract_basic_cookie(self) -> None:
        """Test extracting attributes from a basic cookie."""
        attrs = extract_cookie_attributes("session=abc123", "session")
        assert attrs["value"] == "session=abc123"
        assert attrs["domain"] is None
        assert attrs["max_age"] is None
        assert attrs["expires_at"] is None
        assert attrs["path"] is None
        assert attrs["secure"] is False
        assert attrs["httponly"] is False
        assert attrs["samesite"] is None

    def test_extract_cookie_with_max_age(self) -> None:
        """Test extracting cookie with Max-Age calculates expiration."""
        attrs = extract_cookie_attributes("session=abc123; Max-Age=3600", "session")
        assert attrs["value"] == "session=abc123"
        assert attrs["max_age"] == 3600
        assert attrs["expires_at"] is not None
        # Check that expires_at is approximately 3600 seconds from now
        now = datetime.now(timezone.utc)
        time_diff = (attrs["expires_at"] - now).total_seconds()
        assert 3595 < time_diff < 3605  # Allow 5 second tolerance

    def test_extract_cookie_with_domain(self) -> None:
        """Test extracting cookie with domain."""
        attrs = extract_cookie_attributes("session=abc123; Domain=.example.com", "session")
        assert attrs["domain"] == ".example.com"

    def test_extract_cookie_with_all_attributes(self) -> None:
        """Test extracting cookie with all attributes."""
        attrs = extract_cookie_attributes(
            "orchestrator_session=jwt123; Max-Age=900; Domain=.nannos.rcplus.io; Path=/api; Secure; HttpOnly; SameSite=Lax",
            "orchestrator_session",
        )
        assert attrs["value"] == "orchestrator_session=jwt123"
        assert attrs["domain"] == ".nannos.rcplus.io"
        assert attrs["max_age"] == 900
        assert attrs["path"] == "/api"
        assert attrs["secure"] is True
        assert attrs["httponly"] is True
        assert attrs["samesite"] == "Lax"
        assert attrs["expires_at"] is not None

    def test_extract_cookie_not_found_raises_error(self) -> None:
        """Test that extracting non-existent cookie raises ValueError."""
        with pytest.raises(ValueError, match="Cookie 'missing' not found"):
            extract_cookie_attributes("session=abc123", "missing")

    def test_extract_cookie_invalid_max_age(self) -> None:
        """Test extracting cookie with invalid Max-Age."""
        attrs = extract_cookie_attributes("session=abc123; Max-Age=invalid", "session")
        assert attrs["max_age"] is None
        assert attrs["expires_at"] is None

    def test_extract_orchestrator_session_cookie(self) -> None:
        """Test extracting real orchestrator session cookie."""
        # Simulate actual orchestrator cookie
        cookie_header = (
            "orchestrator_session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9; "
            "Max-Age=900; Domain=.nannos.rcplus.io; Path=/; Secure; HttpOnly; SameSite=Lax"
        )
        attrs = extract_cookie_attributes(cookie_header, "orchestrator_session")
        assert "orchestrator_session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" in attrs["value"]
        assert attrs["domain"] == ".nannos.rcplus.io"
        assert attrs["max_age"] == 900
        assert attrs["secure"] is True
        assert attrs["httponly"] is True


class TestFindCookieInHeaders:
    """Tests for find_cookie_in_headers function."""

    def test_find_cookie_in_single_header(self) -> None:
        """Test finding cookie in a single header."""
        headers = ["session=abc123; Max-Age=3600"]
        found = find_cookie_in_headers(headers, "session")
        assert found == "session=abc123; Max-Age=3600"

    def test_find_cookie_in_multiple_headers(self) -> None:
        """Test finding specific cookie among multiple headers."""
        headers = [
            "other=xyz789; Max-Age=7200",
            "session=abc123; Max-Age=3600",
            "tracking=def456; Max-Age=86400",
        ]
        found = find_cookie_in_headers(headers, "session")
        assert found == "session=abc123; Max-Age=3600"

    def test_find_cookie_not_present(self) -> None:
        """Test finding cookie that doesn't exist returns None."""
        headers = ["session=abc123", "other=xyz789"]
        found = find_cookie_in_headers(headers, "missing")
        assert found is None

    def test_find_cookie_empty_headers(self) -> None:
        """Test finding cookie in empty headers list."""
        found = find_cookie_in_headers([], "session")
        assert found is None

    def test_find_orchestrator_session_cookie(self) -> None:
        """Test finding orchestrator_session among multiple cookies."""
        headers = [
            "session=user123; Path=/",
            "orchestrator_session=jwt456; Max-Age=900; Domain=.nannos.rcplus.io",
            "csrf=token789; Path=/",
        ]
        found = find_cookie_in_headers(headers, "orchestrator_session")
        assert found == "orchestrator_session=jwt456; Max-Age=900; Domain=.nannos.rcplus.io"

    def test_find_cookie_partial_name_match(self) -> None:
        """Test that partial name matches don't return false positives."""
        headers = [
            "user_session=abc123",
            "session=xyz789",
        ]
        # Should find exact match, not 'user_session'
        found = find_cookie_in_headers(headers, "session")
        assert found == "session=xyz789"


class TestValidateCookieDomain:
    """Tests for validate_cookie_domain function."""

    def test_validate_exact_match(self) -> None:
        """Test validation with exact domain match."""
        assert validate_cookie_domain("example.com", "example.com") is True

    def test_validate_subdomain_with_dot_prefix(self) -> None:
        """Test validation with .domain prefix allows subdomains."""
        assert validate_cookie_domain(".example.com", "api.example.com") is True
        assert validate_cookie_domain(".example.com", "www.example.com") is True
        assert validate_cookie_domain(".example.com", "example.com") is True

    def test_validate_subdomain_without_dot_prefix(self) -> None:
        """Test validation without dot prefix."""
        assert validate_cookie_domain("example.com", "api.example.com") is True
        assert validate_cookie_domain("example.com", "example.com") is True

    def test_validate_none_domain_always_valid(self) -> None:
        """Test that None domain is always valid."""
        assert validate_cookie_domain(None, "example.com") is True
        assert validate_cookie_domain(None, "api.example.com") is True

    def test_validate_domain_mismatch(self) -> None:
        """Test validation fails with mismatched domains."""
        assert validate_cookie_domain("example.com", "different.com") is False
        assert validate_cookie_domain(".example.com", "other.com") is False

    def test_validate_orchestrator_domain(self) -> None:
        """Test validation for orchestrator domain scenarios."""
        # Orchestrator cookie with .nannos.rcplus.io should work for api.nannos.rcplus.io
        assert validate_cookie_domain(".nannos.rcplus.io", "api.nannos.rcplus.io") is True
        assert validate_cookie_domain(".nannos.rcplus.io", "orchestrator.nannos.rcplus.io") is True

        # But not for different domain
        assert validate_cookie_domain(".nannos.rcplus.io", "example.com") is False

    def test_validate_localhost(self) -> None:
        """Test validation for localhost scenarios."""
        assert validate_cookie_domain("localhost", "localhost") is True
        assert validate_cookie_domain(None, "localhost") is True
        assert validate_cookie_domain("localhost", "127.0.0.1") is False


class TestCookieAttributesType:
    """Tests for CookieAttributes TypedDict."""

    def test_cookie_attributes_structure(self) -> None:
        """Test that CookieAttributes has expected structure."""
        attrs: CookieAttributes = {
            "value": "session=abc123",
            "domain": ".example.com",
            "max_age": 3600,
            "expires_at": datetime.now(timezone.utc),
            "path": "/",
            "secure": True,
            "httponly": True,
            "samesite": "Lax",
        }
        assert attrs["value"] == "session=abc123"
        assert attrs["domain"] == ".example.com"
        assert attrs["max_age"] == 3600
        assert attrs["expires_at"] is not None
        assert attrs["path"] == "/"
        assert attrs["secure"] is True
        assert attrs["httponly"] is True
        assert attrs["samesite"] == "Lax"
