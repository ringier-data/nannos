"""In-memory cache for tool risk scores with LRU eviction and periodic refresh.

The cache is a process-level singleton shared across all GraphRuntimeContext instances.
It provides:
- Per-record TTL (30 min) based on `updated_at`
- Schema hash validation (mismatch = miss)
- LRU eviction when max_entries exceeded
- Periodic paginated refresh from console-backend API (merge, not swap)
- Pre-compiled glob patterns for fast argument matching
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a glob pattern to a compiled regex.

    Supports: * (any chars), ? (single char), [abc] (char set).
    """
    i, n = 0, len(pattern)
    parts: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            parts.append(".*")
            i += 1
        elif c == "?":
            parts.append(".")
            i += 1
        elif c == "[":
            j = i + 1
            while j < n and pattern[j] != "]":
                j += 1
            parts.append(pattern[i : j + 1])
            i = j + 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile(f"^{''.join(parts)}$", re.IGNORECASE)


@dataclass
class ParamRiskProfile:
    """Risk profile for a single tool parameter."""

    risky_values: dict[str, float]
    """Glob pattern -> risk score mapping."""

    default_contribution: float = 0.0
    """Risk contribution when no pattern matches for this param."""


@dataclass
class ToolRiskEntry:
    """Cached risk score entry for a single tool."""

    base_score: float
    """Inherent risk when no risky patterns match (0.0-1.0)."""

    risk_factors: dict[str, ParamRiskProfile]
    """Keyed by param name. Params here are 'control params'."""

    allowed_actions: list[str]
    """Actions available in the HITL widget (e.g., ["approve", "edit", "reject"])."""

    schema_hash: str
    """Hash of tool's input schema at scoring time."""

    updated_at: datetime
    """When this entry was last loaded/verified — controls TTL."""

    last_accessed_at: datetime
    """When get() last returned this entry — controls LRU eviction."""

    _compiled_patterns: dict[str, list[tuple[re.Pattern[str], float]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    """Pre-compiled glob patterns per param. Not persisted."""

    def compile_patterns(self) -> None:
        """Pre-compile glob patterns for fast matching at execution time."""
        self._compiled_patterns = {}
        for param_name, profile in self.risk_factors.items():
            compiled = []
            for glob_pattern, score in profile.risky_values.items():
                try:
                    compiled.append((_glob_to_regex(glob_pattern), score))
                except re.error:
                    logger.warning("Invalid glob pattern '%s' for param '%s', skipping", glob_pattern, param_name)
            self._compiled_patterns[param_name] = compiled

    def match_args(self, args: dict[str, Any]) -> float:
        """Match tool call args against risk factors. Returns effective risk score.

        For each control param present in args, tests its value against compiled
        glob patterns. If a pattern matches, uses that score. If the param is present
        but no patterns match, applies default_contribution. Returns the highest
        score across all params, floored at base_score.
        """
        if not self._compiled_patterns:
            self.compile_patterns()

        highest_score = self.base_score
        for param_name, patterns in self._compiled_patterns.items():
            arg_value = args.get(param_name)
            if arg_value is None:
                continue
            arg_str = str(arg_value)
            matched = False
            for pattern_re, score in patterns:
                if pattern_re.match(arg_str):
                    highest_score = max(highest_score, score)
                    matched = True
            if not matched:
                profile = self.risk_factors.get(param_name)
                if profile and profile.default_contribution:
                    highest_score = max(highest_score, profile.default_contribution)
        return highest_score

    def get_matched_pattern(self, args: dict[str, Any]) -> str | None:
        """Return the glob pattern string that matched (for UI display), or None."""
        if not self._compiled_patterns:
            self.compile_patterns()

        best_score = self.base_score
        best_pattern: str | None = None
        for param_name, profile in self.risk_factors.items():
            arg_value = args.get(param_name)
            if arg_value is None:
                continue
            arg_str = str(arg_value)
            for glob_pattern, score in profile.risky_values.items():
                compiled = _glob_to_regex(glob_pattern)
                if compiled.match(arg_str) and score > best_score:
                    best_score = score
                    best_pattern = f"{param_name} matches `{glob_pattern}`"
        return best_pattern


class RiskScoreAPIClient(Protocol):
    """Protocol for the console-backend risk scores API client."""

    async def get_scores_paginated(self, limit: int, offset: int) -> Sequence[dict[str, Any]]:
        """Fetch paginated scores sorted by updated_at desc."""
        ...

    async def get_score(self, tool_name: str, server_slug: str) -> dict[str, Any] | None:
        """Fetch a single score by tool_name and server_slug."""
        ...

    async def upsert_score(self, data: dict[str, Any]) -> None:
        """Upsert a risk score entry."""
        ...


class ToolRiskCache:
    """In-memory LRU cache for tool risk scores with periodic refresh.

    Thread-safe for concurrent reads. Writes (put/refresh) are additive merges.
    """

    def __init__(
        self,
        *,
        max_entries: int = 500,
        record_ttl: timedelta = timedelta(minutes=30),
        refresh_interval: timedelta = timedelta(minutes=5),
    ) -> None:
        self._entries: dict[str, ToolRiskEntry] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._api_client: RiskScoreAPIClient | None = None
        self.max_entries = max_entries
        self.record_ttl = record_ttl
        self.refresh_interval = refresh_interval

    def get(self, tool_name: str, server_slug: str, current_schema_hash: str) -> ToolRiskEntry | None:
        """Get a cached entry. Returns None if schema changed or absent.

        When an entry is past TTL but the schema hash still matches (tool unchanged),
        the entry is considered valid — the refresh loop handles content updates from
        the DB. This prevents unnecessary LLM re-scoring for stable tools.

        Updates last_accessed_at on hit.
        """
        key = f"{tool_name}::{server_slug}"
        entry = self._entries.get(key)
        if entry is None:
            return None

        # Schema mismatch = miss (tool schema changed, needs re-score)
        if entry.schema_hash and current_schema_hash and entry.schema_hash != current_schema_hash:
            return None

        # TTL check — but only force miss if we can't confirm schema is unchanged.
        # If the schema hash matches (or either is empty so we can't compare),
        # trust the entry. The periodic refresh loop keeps content fresh from the DB.
        if not self.is_record_fresh(entry):
            # Schema hash confirms tool unchanged — extend TTL, no re-score needed
            if self._schema_confirms_unchanged(entry.schema_hash, current_schema_hash):
                entry.updated_at = datetime.now(timezone.utc)
            else:
                return None

        # Update LRU timestamp
        entry.last_accessed_at = datetime.now(timezone.utc)
        return entry

    @staticmethod
    def _schema_confirms_unchanged(entry_hash: str, current_hash: str) -> bool:
        """Check if schema hashes confirm the tool is unchanged.

        Returns True when:
        - Both hashes match (tool schema verified identical)
        - Either hash is empty (can't disprove, trust the entry)
        """
        if not entry_hash or not current_hash:
            return True
        return entry_hash == current_hash

    def put(self, tool_name: str, server_slug: str, entry: ToolRiskEntry) -> None:
        """Insert or update a cache entry. Evicts LRU entry if at capacity."""
        key = f"{tool_name}::{server_slug}"
        entry.updated_at = datetime.now(timezone.utc)
        entry.last_accessed_at = datetime.now(timezone.utc)
        entry.compile_patterns()

        self._entries[key] = entry

        # LRU eviction if over capacity
        if len(self._entries) > self.max_entries:
            self._evict_lru()

    def persist_entry(self, tool_name: str, server_slug: str, entry: ToolRiskEntry) -> None:
        """Fire-and-forget persist a new/updated entry to the backend API.

        Called by the scorer after LLM scoring to write-through to the database.
        Does nothing if no API client is configured.
        """
        if self._api_client is None:
            return

        data = {
            "tool_name": tool_name,
            "server_slug": server_slug,
            "schema_hash": entry.schema_hash,
            "base_score": entry.base_score,
            "risk_factors": {
                param_name: {
                    "risky_values": profile.risky_values,
                    "default_contribution": profile.default_contribution,
                }
                for param_name, profile in entry.risk_factors.items()
            },
            "allowed_actions": entry.allowed_actions,
        }

        asyncio.create_task(self._persist_entry_async(data))

    async def _persist_entry_async(self, data: dict[str, Any]) -> None:
        """Background task to persist an entry to the API."""
        try:
            await self._api_client.upsert_score(data)  # type: ignore[union-attr]
            logger.debug("Persisted risk score for %s::%s", data["tool_name"], data["server_slug"])
        except Exception:
            logger.warning(
                "Failed to persist risk score for %s::%s", data["tool_name"], data["server_slug"], exc_info=True
            )

    def is_record_fresh(self, entry: ToolRiskEntry) -> bool:
        """Check if a record is within its TTL."""
        now = datetime.now(timezone.utc)
        return (now - entry.updated_at) < self.record_ttl

    async def start(self, api_client: RiskScoreAPIClient) -> None:
        """Load initial data from API and start periodic refresh loop."""
        self._api_client = api_client
        await self._load_from_api()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Cancel the periodic refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

    async def _load_from_api(self) -> None:
        """Paginated load: pages of 100 until max_entries or exhausted."""
        if self._api_client is None:
            return

        offset = 0
        loaded = 0
        while loaded < self.max_entries:
            try:
                page = await self._api_client.get_scores_paginated(limit=100, offset=offset)
            except Exception:
                logger.exception("Failed to load risk scores from API (offset=%d)", offset)
                break

            if not page:
                break

            for row in page:
                if loaded >= self.max_entries:
                    break
                entry = _dict_to_entry(row)
                key = f"{row['tool_name']}::{row['server_slug']}"
                self._entries[key] = entry
                loaded += 1

            offset += len(page)
            if len(page) < 100:
                break

        logger.info("ToolRiskCache loaded %d entries from API", loaded)

    async def _refresh_loop(self) -> None:
        """Periodic refresh: paginated GET, merge into existing dict."""
        while True:
            await asyncio.sleep(self.refresh_interval.total_seconds())
            try:
                await self._do_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ToolRiskCache refresh failed")

    async def _do_refresh(self) -> None:
        """Fetch up to max_entries from API and merge into cache."""
        if self._api_client is None:
            return

        offset = 0
        fetched = 0
        while fetched < self.max_entries:
            try:
                page = await self._api_client.get_scores_paginated(limit=100, offset=offset)
            except Exception:
                logger.exception("Failed during refresh (offset=%d)", offset)
                break

            if not page:
                break

            for row in page:
                if fetched >= self.max_entries:
                    break
                entry = _dict_to_entry(row)
                key = f"{row['tool_name']}::{row['server_slug']}"
                # Merge: update existing or add new, never remove
                existing = self._entries.get(key)
                if existing is not None:
                    # Preserve last_accessed_at (LRU), update score data
                    entry.last_accessed_at = existing.last_accessed_at
                self._entries[key] = entry
                fetched += 1

            offset += len(page)
            if len(page) < 100:
                break

        # Trim if over capacity after merge
        while len(self._entries) > self.max_entries:
            self._evict_lru()

        logger.debug("ToolRiskCache refreshed, %d entries total", len(self._entries))

    def _evict_lru(self) -> None:
        """Remove the entry with the oldest last_accessed_at."""
        if not self._entries:
            return
        oldest_key = min(self._entries, key=lambda k: self._entries[k].last_accessed_at)
        del self._entries[oldest_key]


def _dict_to_entry(row: dict[str, Any]) -> ToolRiskEntry:
    """Convert an API response dict to a ToolRiskEntry."""
    now = datetime.now(timezone.utc)

    # Parse risk_factors from JSONB
    raw_factors = row.get("risk_factors", {})
    risk_factors: dict[str, ParamRiskProfile] = {}
    if isinstance(raw_factors, dict):
        for param_name, profile_data in raw_factors.items():
            if isinstance(profile_data, dict):
                risk_factors[param_name] = ParamRiskProfile(
                    risky_values=profile_data.get("risky_values", {}),
                    default_contribution=profile_data.get("default_contribution", 0.0),
                )

    # Parse updated_at
    updated_at = row.get("updated_at")
    if isinstance(updated_at, str):
        updated_at = datetime.fromisoformat(updated_at)
    elif not isinstance(updated_at, datetime):
        updated_at = now

    entry = ToolRiskEntry(
        base_score=float(row.get("base_score", 0.5)),
        risk_factors=risk_factors,
        allowed_actions=row.get("allowed_actions", ["approve", "edit", "reject"]),
        schema_hash=row.get("schema_hash", ""),
        updated_at=updated_at,
        last_accessed_at=now,
    )
    entry.compile_patterns()
    return entry
