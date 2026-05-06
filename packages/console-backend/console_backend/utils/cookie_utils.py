"""Cookie parsing utilities using Python's http.cookies module.

This module provides utilities for parsing and extracting cookie attributes
from Set-Cookie headers using Python's built-in SimpleCookie class.
"""

from __future__ import annotations

import logging

from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import TypedDict


logger = logging.getLogger(__name__)


class CookieAttributes(TypedDict):
    """Type definition for cookie attributes."""

    value: str
    domain: str | None
    max_age: int | None
    expires_at: datetime | None
    path: str | None
    secure: bool
    httponly: bool
    samesite: str | None


def parse_set_cookie(set_cookie_header: str) -> SimpleCookie:
    """Parse a Set-Cookie header into a SimpleCookie object.

    Args:
        set_cookie_header: The raw Set-Cookie header value

    Returns:
        SimpleCookie object with parsed cookie data

    Example:
        >>> cookie = parse_set_cookie('session=abc123; Max-Age=3600; Domain=.example.com')
        >>> cookie['session'].value
        'abc123'
        >>> cookie['session']['max-age']
        '3600'
    """
    cookie = SimpleCookie()
    cookie.load(set_cookie_header)
    return cookie


def extract_cookie_attributes(
    set_cookie_header: str,
    cookie_name: str,
) -> CookieAttributes:
    """Extract cookie value and attributes from a Set-Cookie header.

    Args:
        set_cookie_header: The raw Set-Cookie header value
        cookie_name: The name of the cookie to extract

    Returns:
        CookieAttributes dictionary containing:
        - value: The cookie value (name=value part)
        - domain: The cookie domain (if present)
        - max_age: The max-age in seconds as int (if present)
        - expires_at: Calculated expiration datetime (if max-age present)
        - path: The cookie path (if present)
        - secure: Whether cookie is secure (if present)
        - httponly: Whether cookie is httponly (if present)
        - samesite: The samesite attribute (if present)

    Raises:
        ValueError: If cookie_name is not found in the Set-Cookie header

    Example:
        >>> attrs = extract_cookie_attributes(
        ...     'session=abc123; Max-Age=3600; Domain=.example.com; Path=/; Secure; HttpOnly', 'session'
        ... )
        >>> attrs['value']
        'session=abc123'
        >>> attrs['domain']
        '.example.com'
        >>> attrs['max_age']
        3600
    """
    cookie = parse_set_cookie(set_cookie_header)

    if cookie_name not in cookie:
        raise ValueError(f"Cookie '{cookie_name}' not found in Set-Cookie header")

    morsel = cookie[cookie_name]

    # Extract max-age and calculate expiration
    max_age = None
    expires_at = None
    if morsel.get('max-age'):
        try:
            max_age = int(morsel['max-age'])
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=max_age)
        except (ValueError, TypeError):
            logger.warning(f'Invalid max-age value: {morsel.get("max-age")}')

    return CookieAttributes(
        value=f'{cookie_name}={morsel.value}',
        domain=morsel.get('domain') or None,
        max_age=max_age,
        expires_at=expires_at,
        path=morsel.get('path') or None,
        secure=bool(morsel.get('secure')),
        httponly=bool(morsel.get('httponly')),
        samesite=morsel.get('samesite') or None,
    )


def find_cookie_in_headers(
    set_cookie_headers: list[str],
    cookie_name: str,
) -> str | None:
    """Find a specific cookie by name in a list of Set-Cookie headers.

    Args:
        set_cookie_headers: List of Set-Cookie header values
        cookie_name: The name of the cookie to find

    Returns:
        The Set-Cookie header containing the cookie, or None if not found

    Example:
        >>> headers = ['session=abc123; Max-Age=3600', 'other=xyz789; Max-Age=7200']
        >>> find_cookie_in_headers(headers, 'session')
        'session=abc123; Max-Age=3600'
    """
    for header in set_cookie_headers:
        # Quick check before full parse
        if f'{cookie_name}=' in header:
            try:
                cookie = parse_set_cookie(header)
                if cookie_name in cookie:
                    return header
            except Exception as e:
                logger.debug(f'Failed to parse Set-Cookie header: {e}')
                continue

    return None


def validate_cookie_domain(
    cookie_domain: str | None,
    expected_domain: str,
) -> bool:
    """Validate that a cookie's domain matches the expected domain.

    Handles domain prefixes (e.g., .example.com) and subdomain matching.

    Args:
        cookie_domain: The domain from the cookie (can be None or have . prefix)
        expected_domain: The expected domain to validate against

    Returns:
        True if domains match, False otherwise

    Example:
        >>> validate_cookie_domain('.example.com', 'api.example.com')
        True
        >>> validate_cookie_domain('example.com', 'other.com')
        False
    """
    if not cookie_domain:
        # No domain specified means cookie is valid for request domain
        return True

    # Domain can be prefixed with . (e.g., .example.com)
    cookie_domain_clean = cookie_domain.lstrip('.')
    expected_domain_clean = expected_domain.lstrip('.')

    # Check if expected domain ends with cookie domain (allows subdomains)
    return expected_domain_clean.endswith(cookie_domain_clean)
