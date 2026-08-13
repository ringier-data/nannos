"""Timezone resolution helpers.

The system-wide default timezone is controlled exclusively by the
``DEFAULT_TIMEZONE`` environment variable (set per deployment); code must never
hardcode a specific locale. The fallback when the variable is unset is plain
UTC. It is read at call time (not import time) so a job row that stores no
timezone keeps following the deployment configuration.
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def default_timezone_name() -> str:
    """The deployment-wide default IANA timezone (env-controlled, never hardcoded)."""
    return os.getenv("DEFAULT_TIMEZONE", "UTC")


def validate_timezone_name(name: str | None) -> str | None:
    """Return *name* if zoneinfo can resolve it, raising ValueError otherwise.

    ``ZoneInfoNotFoundError`` subclasses KeyError, not ValueError — callers that
    map ValueError to HTTP 400 would otherwise let it escape as a 500.
    """
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown IANA timezone: {name!r}") from e
    return name


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Resolve an optional timezone name to a ZoneInfo, raising ValueError if invalid.

    None or empty/blank strings fall back to the deployment default — the two
    shapes a job row without an explicit timezone can carry.
    """
    effective = (name or "").strip() or default_timezone_name()
    try:
        return ZoneInfo(effective)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown IANA timezone: {effective!r}") from e
