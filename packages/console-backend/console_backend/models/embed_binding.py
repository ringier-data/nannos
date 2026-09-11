"""Embed bindings: a sub-agent whose definition is published by a host (ADR-0006).

An admin binds an existing sub-agent to a host with two facts: the OAuth client ids
(``azp``) whose tokens belong to that host, and the base URL that serves
``/.well-known/agent-skills/``. console-backend keeps the sub-agent's config versions in
sync with what the host publishes and activates every arriving user whose token carries a
bound ``azp``.
"""

from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

#: Keycloak client ids: letters, digits, dot, underscore, colon, hyphen.
AZP_MAX_LENGTH = 255


def normalize_base_url(value: str) -> str:
    """Validate the structural shape of a base URL and return it without a trailing slash.

    Scheme must be http or https, a host must be present, and there must be no path,
    query or fragment — the well-known path is appended by the consumer. Whether plain
    http is *allowed* (local development only) is decided by the service, which knows
    the environment.
    """
    candidate = (value or "").strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            "base_url must start with https:// (http:// only for localhost in local development)"
        )
    if not parts.netloc or parts.username or parts.password:
        raise ValueError(
            "base_url must be an origin such as https://riad.alloy.ch, without credentials"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("base_url must be an origin only — no path, query or fragment")
    return f"{parts.scheme}://{parts.netloc}"


def is_loopback_host(base_url: str) -> bool:
    """True for http(s)://localhost[:port] and 127.0.0.1[:port]."""
    host = urlsplit(base_url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


class EmbedBindingUpsert(BaseModel):
    """Admin request body for PUT /api/v1/sub-agents/{id}/embed-binding."""

    base_url: str = Field(
        description="Origin that serves /.well-known/agent-skills/, e.g. https://riad.alloy.ch"
    )
    azps: list[str] = Field(
        min_length=1,
        description="OAuth client ids (token `azp`) whose users are bound to this sub-agent",
    )

    @field_validator("base_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("azps")
    @classmethod
    def _clean_azps(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            azp = (raw or "").strip()
            if not azp:
                raise ValueError("azps must not contain empty values")
            if len(azp) > AZP_MAX_LENGTH:
                raise ValueError(
                    f"azp '{azp[:32]}…' is longer than {AZP_MAX_LENGTH} characters"
                )
            if any(ch.isspace() for ch in azp):
                raise ValueError(f"azp '{azp}' must not contain whitespace")
            if azp not in cleaned:
                cleaned.append(azp)
        return cleaned


class EmbedBindingProbeRequest(BaseModel):
    """Admin request body for POST /api/v1/sub-agents/embed-bindings/probe.

    `base_url` is deliberately NOT validated here: the probe is a test, so a malformed
    origin must come back as a readable probe failure, not as a 422 the caller has to
    unwrap. The service normalizes it and reports the same message inline.
    """

    base_url: str = Field(
        description="Origin to read /.well-known/agent-skills/ from, e.g. https://riad.alloy.ch"
    )


class WellKnownSkillInfo(BaseModel):
    """One published skill as listed in the host's index (no body — that lives in the version)."""

    name: str
    url: str
    digest: str


class WellKnownAgentInfo(BaseModel):
    """The `x-nannos-agent` block as last synced, for the admin view."""

    name: str
    description: str
    organization: str | None = None
    prompt_url: str
    prompt_digest: str
    tools: list[str] | None = None
    model_tier: str | None = None
    thinking_level: str | None = None


class EmbedBinding(BaseModel):
    """Read model: the binding plus the state of its last sync."""

    sub_agent_id: int
    base_url: str
    index_url: str
    azps: list[str]
    revision: str | None = None
    version_hash: str | None = None
    fetched_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_seen_at: datetime | None = None
    azps_seen: dict[str, str] = Field(default_factory=dict)
    agent: WellKnownAgentInfo | None = None
    skills: list[WellKnownSkillInfo] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_at: datetime


class EmbedBindingProbe(BaseModel):
    """Result of a dry run against an authority. Nothing is created or changed.

    `ok` is False when the authority could not be read; `error` then says why, in the
    same words the create call would have failed with.
    """

    ok: bool
    base_url: str
    index_url: str
    agent: WellKnownAgentInfo | None = None
    skills: list[WellKnownSkillInfo] = Field(default_factory=list)
    revision: str | None = None
    error: str | None = None
