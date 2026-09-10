"""Fetch and validate a host-published agent definition (ADR-0006).

A first-party host publishes, on its own origin:

    /.well-known/agent-skills/index.json      Agent Skills Discovery RFC 0.2.0 index
                                              + an `x-nannos-agent` extension block
    /.well-known/agent-skills/AGENT.md        frontmatter + the system prompt body
    /.well-known/agent-skills/<name>/SKILL.md one agentskills.io skill per directory

Every file the index points at carries a `sha256:` digest over the exact bytes served.
The `x-nannos-agent` block carries name, description and the prompt; `tools`, `model_tier`,
`thinking_level` and `organization` are optional — whatever the host leaves out stays a
Nannos-side setting on the bound sub-agent. When neither side sets a tool list, the bound
sub-agent runs with every tool the user has (the orchestrator gives it the general-purpose
agent's lazy catalog); a published list narrows that.

This module fetches the tree, verifies each digest, validates the shapes, and returns one
immutable `WellKnownDefinition` whose `revision` changes when any byte — or the Nannos
framing template — changes. It never touches the database; `EmbedBindingService` turns a
definition into a sub-agent config version.

Trust model: the base URL is admin configuration. Outside local development the host must
resolve to public addresses only: private, loopback and link-local destinations are
refused before the first request, so an admin cannot point Nannos at the cluster's own
services. Redirects are followed only within the same origin, every response is
size-capped, and the index is trusted no more than the files it names (a digest mismatch
is a hard error for that fetch).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, Field

from console_backend.models.skills_registry import (
    MAX_SKILL_FILE_SIZE_BYTES,
    MAX_SKILL_FILES,
)
from console_backend.models.sub_agent import SUB_AGENT_NAME_RE, ModelTier, ThinkingLevel

logger = logging.getLogger(__name__)

WELL_KNOWN_INDEX_PATH = "/.well-known/agent-skills/index.json"
SUPPORTED_INDEX_SCHEMAS = frozenset(
    {"https://schemas.agentskills.io/discovery/0.2.0/schema.json"}
)
AGENT_EXTENSION_KEY = "x-nannos-agent"
SKILL_TYPE = "skill-md"

#: Bump when `render_embed_framing` changes wording, so bound sub-agents re-sync a new
#: version even though the host published nothing new.
FRAMING_TEMPLATE_VERSION = "1"

#: `thinking_level` value that disables extended thinking (the enum has no "off").
THINKING_OFF = "off"

MAX_INDEX_BYTES = 64 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 10.0
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
DEFAULT_TTL_SECONDS = 300

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_ORGANIZATION_LENGTH = 200

_SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)
_NAME_SEPARATORS = re.compile(r"[^A-Za-z0-9_-]+")
_LEADING_NON_LETTER = re.compile(r"^[^A-Za-z]+")

#: Used when a published name carries no letter at all ("123", "!!!"), so the sync still
#: has something to create the sub-agent row with.
FALLBACK_AGENT_NAME = "embedded-agent"
_MAX_AGE = re.compile(r"max-age\s*=\s*(\d+)", re.IGNORECASE)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class WellKnownFetchError(Exception):
    """One fetch of a host definition failed. `step` names where; `detail` says why."""

    def __init__(self, base_url: str, step: str, detail: str) -> None:
        self.base_url = base_url
        self.step = step
        self.detail = detail
        super().__init__(f"{step}: {detail}")


class WellKnownSkill(BaseModel):
    name: str
    description: str
    body: str = Field(description="SKILL.md body, frontmatter stripped")
    url: str
    digest: str


def to_sub_agent_name(published_name: str) -> str:
    """Turn a host's display name into a name a sub-agent row can hold.

    The host publishes a name for people to read ("Alloy AI Assistant"). A sub-agent name
    is also the identifier the orchestrator puts in its task tool enum, so it must match
    `^[a-zA-Z][a-zA-Z0-9_-]*$` — no spaces, no leading digit, 64 chars at most. Rather than
    refuse the definition over a cosmetic field, derive a compliant name from it: runs of
    other characters become one hyphen, and anything before the first letter is dropped.

    The display name is kept as published and is what the framing prompt and the admin
    view show; only the row name is derived.
    """
    slug = _NAME_SEPARATORS.sub("-", published_name.strip())
    slug = _LEADING_NON_LETTER.sub("", slug)
    slug = slug[:MAX_NAME_LENGTH].rstrip("-_")
    # The console's own rule is the authority: never hand a row a name it would refuse,
    # or the orchestrator's registry guard silently drops the agent for every user.
    if not slug or not SUB_AGENT_NAME_RE.fullmatch(slug):
        return FALLBACK_AGENT_NAME
    return slug


class WellKnownAgent(BaseModel):
    name: str = Field(description="Display name, exactly as the host published it")
    description: str
    organization: str | None = None
    prompt_body: str = Field(description="AGENT.md body, frontmatter stripped")
    tools: list[str] | None = Field(
        default=None,
        description="Published MCP tool scope; None when the host leaves the tool list to Nannos",
    )
    model_tier: str | None = None
    thinking_level: str | None = Field(
        default=None, description="'off' or a ThinkingLevel value"
    )
    url: str
    digest: str

    @property
    def sub_agent_name(self) -> str:
        """`name`, reduced to something a sub-agent row can hold. See to_sub_agent_name."""
        return to_sub_agent_name(self.name)


class WellKnownDefinition(BaseModel):
    base_url: str
    index_url: str
    agent: WellKnownAgent
    skills: list[WellKnownSkill]
    revision: str = Field(
        description=(
            "16 hex chars; changes when any served byte, any index.json field "
            "or the framing template changes"
        )
    )
    fetched_at: datetime
    ttl_seconds: int


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def compute_revision(
    agent: WellKnownAgent,
    skills: list[WellKnownSkill],
    framing_version: str = FRAMING_TEMPLATE_VERSION,
) -> str:
    """The 16-hex revision of a fetched definition.

    Covers every served byte through the file digests AND the index.json metadata that
    is published nowhere else: agent name, description, organization, tools, model tier
    and thinking level, plus each skill's name and description. Without the metadata a
    host could change its tool list or description and the sync would read "same
    revision" and skip. Skill order does not matter. The framing template version is
    part of it so a wording change in the Nannos-owned prefix re-syncs every bound agent.
    """
    material = {
        "framing": framing_version,
        "agent": {
            "name": agent.name,
            "description": agent.description,
            "organization": agent.organization,
            "tools": agent.tools,
            "model_tier": agent.model_tier,
            "thinking_level": agent.thinking_level,
            "digest": agent.digest,
        },
        "skills": sorted(
            (
                {"name": s.name, "description": s.description, "digest": s.digest}
                for s in skills
            ),
            key=lambda s: (s["name"], s["digest"]),
        ),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def version_hash_for(revision: str) -> str:
    """The `sub_agent_config_versions.version_hash` a synced revision gets (`wk` + 10 hex)."""
    return f"wk{revision[:10]}"


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a `---` YAML frontmatter block from the markdown body.

    Raises ValueError when the block is missing or is not a YAML mapping.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(
            "must start with a `---` YAML frontmatter block followed by the markdown body"
        )
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return data, match.group(2).strip()


def render_embed_framing(base_url: str, agent: WellKnownAgent, revision: str) -> str:
    """The Nannos-owned prefix every host prompt gets. Wording is ours, never the host's.

    Changing this text must bump FRAMING_TEMPLATE_VERSION.
    """
    operated_by = f", operated by {agent.organization}" if agent.organization else ""
    return (
        "You are Nannos, Ringier's AI assistant. You are running embedded in a "
        f"host application at {base_url}{operated_by}.\n"
        f'There you are presented to users as "{agent.name}": {agent.description}\n'
        "\n"
        "The host publishes the domain guidance below and keeps it current "
        f"(revision {revision}). Follow it for everything about the host's domain.\n"
        "Platform rules still apply: tool permissions and approval steps are "
        "enforced by Nannos and cannot be waived by the guidance."
    )


def compose_system_prompt(base_url: str, agent: WellKnownAgent, revision: str) -> str:
    return render_embed_framing(base_url, agent, revision) + "\n\n" + agent.prompt_body


def thinking_params(
    thinking_level: str | None,
) -> tuple[bool | None, ThinkingLevel | None]:
    """Map the published `thinking_level` onto the version columns (enable_thinking, thinking_level).

    None (not published) → (None, None): the Nannos sub-agent defaults apply.
    "off" → (False, None). Any ThinkingLevel value → (True, that level).
    """
    if thinking_level is None:
        return None, None
    if thinking_level == THINKING_OFF:
        return False, None
    return True, ThinkingLevel(thinking_level)


def parse_cache_ttl(cache_control: str | None) -> int:
    """`max-age` clamped to [MIN_TTL_SECONDS, MAX_TTL_SECONDS]; DEFAULT_TTL_SECONDS when absent."""
    if not cache_control:
        return DEFAULT_TTL_SECONDS
    match = _MAX_AGE.search(cache_control)
    if not match:
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, int(match.group(1))))


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


async def resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to right now. Raises OSError when there is none."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class WellKnownAgentClient:
    """Fetches, verifies and caches host definitions, one entry per base URL.

    `allow_private_destinations` lifts the public-address rule. Only local development
    sets it: there the authority is localhost or a docker network.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        allow_private_destinations: bool = False,
    ) -> None:
        self._client = client
        self._allow_private = allow_private_destinations
        # base_url -> (monotonic expiry, definition)
        self._cache: dict[str, tuple[float, WellKnownDefinition]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Redirects are followed by hand so the same-origin rule can be enforced.
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    def invalidate(self, base_url: str | None = None) -> None:
        if base_url is None:
            self._cache.clear()
        else:
            self._cache.pop(base_url, None)

    async def fetch(self, base_url: str, *, force: bool = False) -> WellKnownDefinition:
        """Return the host's current definition, from cache while its TTL holds.

        Raises WellKnownFetchError on any problem; nothing is cached in that case.
        """
        cached = self._cache.get(base_url)
        if cached and not force and time.monotonic() < cached[0]:
            return cached[1]

        origin = origin_of(base_url)
        # Every request below stays on this origin (index, files, redirects), so one
        # destination check up front covers them all.
        await self._refuse_private_destination(base_url, origin)
        index_url = base_url.rstrip("/") + WELL_KNOWN_INDEX_PATH
        raw_index, headers = await self._get(
            base_url, index_url, origin, MAX_INDEX_BYTES, "index"
        )
        try:
            index = json.loads(raw_index)
        except ValueError as e:
            raise WellKnownFetchError(
                base_url, "index", f"index.json is not valid JSON: {e}"
            ) from e
        agent_meta, skill_entries = _validate_index(base_url, index)

        prompt_url = _resolve_same_origin(
            base_url, index_url, origin, agent_meta["prompt"]["url"], "agent"
        )
        prompt_bytes, _ = await self._get(
            base_url, prompt_url, origin, MAX_SKILL_FILE_SIZE_BYTES, "agent"
        )
        _verify_digest(base_url, "agent", prompt_bytes, agent_meta["prompt"]["digest"])
        prompt_body = _parse_prompt(base_url, prompt_bytes)

        skills: list[WellKnownSkill] = []
        for entry in skill_entries:
            step = f"skill:{entry['name']}"
            skill_url = _resolve_same_origin(
                base_url, index_url, origin, entry["url"], step
            )
            skill_bytes, _ = await self._get(
                base_url, skill_url, origin, MAX_SKILL_FILE_SIZE_BYTES, step
            )
            _verify_digest(base_url, step, skill_bytes, entry["digest"])
            body = _parse_skill(base_url, step, skill_bytes, entry["name"], skill_url)
            skills.append(
                WellKnownSkill(
                    name=entry["name"],
                    description=entry["description"],
                    body=body,
                    url=skill_url,
                    digest=entry["digest"],
                )
            )

        agent = WellKnownAgent(
            name=agent_meta["name"],
            description=agent_meta["description"],
            organization=agent_meta.get("organization"),
            prompt_body=prompt_body,
            tools=list(agent_meta["tools"])
            if agent_meta.get("tools") is not None
            else None,
            model_tier=agent_meta.get("model_tier"),
            thinking_level=agent_meta.get("thinking_level"),
            url=prompt_url,
            digest=agent_meta["prompt"]["digest"],
        )
        ttl = parse_cache_ttl(headers.get("cache-control"))
        definition = WellKnownDefinition(
            base_url=base_url,
            index_url=index_url,
            agent=agent,
            skills=skills,
            revision=compute_revision(agent, skills),
            fetched_at=datetime.now(timezone.utc),
            ttl_seconds=ttl,
        )
        self._cache[base_url] = (time.monotonic() + ttl, definition)
        return definition

    async def _refuse_private_destination(self, base_url: str, origin: str) -> None:
        """Refuse an authority that resolves to any non-public address (SSRF guard).

        Blocks private ranges, loopback, link-local (cloud metadata), IPv4-mapped IPv6
        and the other non-global ranges `ipaddress` knows. The host is resolved here and
        again by httpx when it connects, so a DNS answer that changes in between is not
        caught: the base URL is admin input, so this is defence in depth, not the only
        line.
        """
        if self._allow_private:
            return
        host = urlsplit(origin).hostname or ""
        step = "authority"
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                resolved = await resolve_host(host)
            except OSError as e:
                raise WellKnownFetchError(
                    base_url, step, f"{host} does not resolve: {e}"
                ) from e
            if not resolved:
                raise WellKnownFetchError(base_url, step, f"{host} does not resolve")
            addresses = [ipaddress.ip_address(address) for address in resolved]
        for address in addresses:
            if not address.is_global:
                raise WellKnownFetchError(
                    base_url,
                    step,
                    f"{host} resolves to {address}, which is not a public address; "
                    "an authority must be reachable on the public internet",
                )

    async def _get(
        self, base_url: str, url: str, origin: str, limit: int, step: str
    ) -> tuple[bytes, httpx.Headers]:
        """GET with a size cap, following at most MAX_REDIRECTS same-origin redirects."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with self._http().stream(
                    "GET", current, timeout=FETCH_TIMEOUT_SECONDS
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise WellKnownFetchError(
                                base_url,
                                step,
                                f"{current} redirected without a Location header",
                            )
                        current = _resolve_same_origin(
                            base_url, current, origin, location, step
                        )
                        continue
                    if response.status_code != 200:
                        raise WellKnownFetchError(
                            base_url,
                            step,
                            f"{current} returned HTTP {response.status_code}",
                        )
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise WellKnownFetchError(
                                base_url,
                                step,
                                f"{current} is larger than {limit} bytes",
                            )
                    return bytes(buffer), response.headers
            except httpx.HTTPError as e:
                raise WellKnownFetchError(
                    base_url, step, f"{current}: {e.__class__.__name__}: {e}"
                ) from e
        raise WellKnownFetchError(
            base_url, step, f"{url}: more than {MAX_REDIRECTS} redirects"
        )


def _resolve_same_origin(
    base_url: str, from_url: str, origin: str, target: str, step: str
) -> str:
    if not isinstance(target, str) or not target:
        raise WellKnownFetchError(base_url, step, "url is missing")
    resolved = urljoin(from_url, target)
    if origin_of(resolved) != origin:
        raise WellKnownFetchError(
            base_url,
            step,
            f"{resolved} is not on {origin}; cross-origin references are refused",
        )
    return resolved


def _verify_digest(base_url: str, step: str, data: bytes, expected: str) -> None:
    actual = _sha256_digest(data)
    if actual != expected:
        raise WellKnownFetchError(
            base_url,
            step,
            f"digest mismatch: index says {expected}, served bytes are {actual}",
        )


def _require_str(
    base_url: str,
    step: str,
    obj: dict[str, Any],
    key: str,
    max_length: int,
    *,
    required: bool = True,
) -> str | None:
    value = obj.get(key)
    if value is None:
        if required:
            raise WellKnownFetchError(base_url, step, f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise WellKnownFetchError(base_url, step, f"'{key}' must be a non-empty string")
    if len(value) > max_length:
        raise WellKnownFetchError(
            base_url,
            step,
            f"'{key}' is {len(value)} characters, the maximum is {max_length}",
        )
    return value


def _validate_index(
    base_url: str, index: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    step = "index"
    if not isinstance(index, dict):
        raise WellKnownFetchError(base_url, step, "index.json must be a JSON object")
    schema = index.get("$schema")
    if schema not in SUPPORTED_INDEX_SCHEMAS:
        raise WellKnownFetchError(
            base_url,
            step,
            f"unsupported $schema {schema!r}; supported: {sorted(SUPPORTED_INDEX_SCHEMAS)}",
        )

    raw_skills = index.get("skills")
    if not isinstance(raw_skills, list):
        raise WellKnownFetchError(base_url, step, "'skills' must be a list")
    if len(raw_skills) > MAX_SKILL_FILES:
        raise WellKnownFetchError(
            base_url,
            step,
            f"{len(raw_skills)} skills listed, the maximum is {MAX_SKILL_FILES}",
        )
    skill_entries: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw_skills):
        estep = f"index.skills[{i}]"
        if not isinstance(entry, dict):
            raise WellKnownFetchError(base_url, estep, "must be an object")
        if entry.get("type") != SKILL_TYPE:
            raise WellKnownFetchError(base_url, estep, f"type must be '{SKILL_TYPE}'")
        name = _require_str(base_url, estep, entry, "name", MAX_NAME_LENGTH) or ""
        if not _SKILL_NAME.match(name):
            raise WellKnownFetchError(
                base_url,
                estep,
                f"name '{name}' must be lowercase letters, digits and single hyphens",
            )
        if name in seen_names:
            raise WellKnownFetchError(
                base_url, estep, f"skill '{name}' is listed twice"
            )
        seen_names.add(name)
        description = (
            _require_str(base_url, estep, entry, "description", MAX_DESCRIPTION_LENGTH)
            or ""
        )
        url = _require_str(base_url, estep, entry, "url", 2048) or ""
        digest = _require_str(base_url, estep, entry, "digest", 80) or ""
        if not _DIGEST.match(digest):
            raise WellKnownFetchError(
                base_url,
                estep,
                "digest must be 'sha256:' followed by 64 lowercase hex characters",
            )
        skill_entries.append(
            {"name": name, "description": description, "url": url, "digest": digest}
        )

    agent = index.get(AGENT_EXTENSION_KEY)
    astep = f"index.{AGENT_EXTENSION_KEY}"
    if not isinstance(agent, dict):
        raise WellKnownFetchError(
            base_url, step, f"'{AGENT_EXTENSION_KEY}' extension block is required"
        )
    _require_str(base_url, astep, agent, "name", MAX_NAME_LENGTH)
    _require_str(base_url, astep, agent, "description", MAX_DESCRIPTION_LENGTH)
    _require_str(
        base_url, astep, agent, "organization", MAX_ORGANIZATION_LENGTH, required=False
    )
    prompt = agent.get("prompt")
    if not isinstance(prompt, dict):
        raise WellKnownFetchError(
            base_url, astep, "'prompt' must be an object with 'url' and 'digest'"
        )
    _require_str(base_url, f"{astep}.prompt", prompt, "url", 2048)
    prompt_digest = (
        _require_str(base_url, f"{astep}.prompt", prompt, "digest", 80) or ""
    )
    if not _DIGEST.match(prompt_digest):
        raise WellKnownFetchError(
            base_url,
            f"{astep}.prompt",
            "digest must be 'sha256:' followed by 64 lowercase hex characters",
        )
    # Optional: a host may leave the MCP tool list to the Nannos side (the cockpit does).
    # With nothing set there either, the bound sub-agent gets every tool the user has.
    tools = agent.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise WellKnownFetchError(
                base_url,
                astep,
                "'tools', when published, must list at least one MCP tool",
            )
        seen_tools: set[str] = set()
        for tool in tools:
            if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
                raise WellKnownFetchError(
                    base_url, astep, f"tool {tool!r} is not a snake_case MCP tool name"
                )
            if tool in seen_tools:
                raise WellKnownFetchError(
                    base_url, astep, f"tool '{tool}' is listed twice"
                )
            seen_tools.add(tool)
    model_tier = agent.get("model_tier")
    if model_tier is not None and model_tier not in {t.value for t in ModelTier}:
        raise WellKnownFetchError(
            base_url,
            astep,
            f"model_tier {model_tier!r} is not one of {[t.value for t in ModelTier]}",
        )
    thinking_level = agent.get("thinking_level")
    allowed_thinking = {THINKING_OFF, *(t.value for t in ThinkingLevel)}
    if thinking_level is not None and thinking_level not in allowed_thinking:
        raise WellKnownFetchError(
            base_url,
            astep,
            f"thinking_level {thinking_level!r} is not one of {sorted(allowed_thinking)}",
        )
    return agent, skill_entries


def _decode(base_url: str, step: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WellKnownFetchError(base_url, step, f"file is not UTF-8: {e}") from e


def _parse_prompt(base_url: str, data: bytes) -> str:
    step = "agent"
    try:
        _, body = split_frontmatter(_decode(base_url, step, data))
    except ValueError as e:
        raise WellKnownFetchError(base_url, step, f"AGENT.md {e}") from e
    if not body:
        raise WellKnownFetchError(
            base_url, step, "AGENT.md has no prompt body after the frontmatter"
        )
    if "{{" in body:
        # The orchestrator substitutes whitelisted {{TOKEN}} placeholders on the composed
        # prompt (registry.resolve_prompt_placeholders). Host prose must not reach them.
        raise WellKnownFetchError(base_url, step, "AGENT.md body must not contain '{{'")
    return body


def _parse_skill(
    base_url: str, step: str, data: bytes, expected_name: str, url: str
) -> str:
    try:
        frontmatter, body = split_frontmatter(_decode(base_url, step, data))
    except ValueError as e:
        raise WellKnownFetchError(base_url, step, f"SKILL.md {e}") from e
    name = frontmatter.get("name")
    if name != expected_name:
        raise WellKnownFetchError(
            base_url,
            step,
            f"SKILL.md frontmatter name {name!r} does not match the index entry '{expected_name}'",
        )
    directory = urlsplit(url).path.rstrip("/").rsplit("/", 2)
    if len(directory) < 2 or directory[-2] != expected_name:
        raise WellKnownFetchError(
            base_url,
            step,
            f"SKILL.md must live in a directory named '{expected_name}' (url is {url})",
        )
    if (
        not isinstance(frontmatter.get("description"), str)
        or not frontmatter["description"].strip()
    ):
        raise WellKnownFetchError(
            base_url, step, "SKILL.md frontmatter 'description' is required"
        )
    if not body:
        raise WellKnownFetchError(
            base_url, step, "SKILL.md has no body after the frontmatter"
        )
    if "{{" in body:
        raise WellKnownFetchError(base_url, step, "SKILL.md body must not contain '{{'")
    return body
