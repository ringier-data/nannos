"""Origin allowlist shared by REST CORS and the Socket.IO handshake.

Chat front-ends that are not the console itself (the embed SDK inside the Alloy
cockpit, for one) run on their own origin and talk to this backend cross-origin
with bearer tokens. Browsers only let them if the backend names their origin, so
``CORS_ALLOWED_CHAT_ORIGINS`` lists them. Two entry forms:

- exact origins: ``https://riad.alloy.ch``, ``http://localhost:3000``
- wildcard patterns: ``https://pr-*-riad.d.alloy.ch`` — ``*`` matches ONE hostname
  label fragment (no dots, no slashes), which covers per-PR preview environments
  without opening whole domains.

Both CORS layers consume the same parsed allowlist so they can never drift apart
(they did once, and every embed host was rejected before auth even ran).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_WILDCARD_FRAGMENT = r"[^./]+"


def _entry_to_regex(entry: str) -> str:
    """Escape an origin pattern and turn each ``*`` into a single-label fragment."""
    return "".join(
        _WILDCARD_FRAGMENT if part == "*" else re.escape(part)
        for part in re.split(r"(\*)", entry)
    )


@dataclass(frozen=True)
class OriginAllowlist:
    exact: tuple[str, ...]
    patterns: tuple[
        str, ...
    ]  # the raw wildcard entries, kept for logging/config display

    def __post_init__(self) -> None:
        compiled = (
            re.compile("|".join(f"(?:{_entry_to_regex(p)})" for p in self.patterns))
            if self.patterns
            else None
        )
        object.__setattr__(self, "_compiled", compiled)

    @property
    def regex(self) -> str | None:
        """Anchored alternation for Starlette's ``allow_origin_regex`` (``None`` without patterns)."""
        compiled: re.Pattern[str] | None = getattr(self, "_compiled")
        return compiled.pattern if compiled else None

    def is_allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        if origin in self.exact:
            return True
        compiled: re.Pattern[str] | None = getattr(self, "_compiled")
        return bool(compiled and compiled.fullmatch(origin))

    def __iter__(self):
        return iter(self.exact)

    def __len__(self) -> int:
        return len(self.exact) + len(self.patterns)


def parse_origin_allowlist(entries: Iterable[str]) -> OriginAllowlist:
    """Split entries into exact origins and wildcard patterns, dropping blanks and duplicates."""
    exact: list[str] = []
    patterns: list[str] = []
    for raw in entries:
        entry = raw.strip().rstrip("/")
        if not entry:
            continue
        bucket = patterns if "*" in entry else exact
        if entry not in bucket:
            bucket.append(entry)
    return OriginAllowlist(exact=tuple(exact), patterns=tuple(patterns))
