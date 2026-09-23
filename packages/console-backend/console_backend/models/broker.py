"""Token broker models: registered broker clients, and the redeem / mint contract.

A broker client is a Keycloak client (a chat client such as ``slack-client``, or the
cockpit BFF's ``cockpit-embed``) that sends its users through console-backend's login
instead of running its own. The registration says where the browser may be sent back to.
Which audiences a client may have tokens minted for is not registered: see
``BrokerService.mint``.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

#: What a `*` in a registered redirect URI's first host label may stand for: DNS label
#: characters only. Stricter than the CORS allowlist's fragment on purpose: a redirect
#: target that let `*` absorb `@` or `:` would turn userinfo tricks into an open redirect.
_HOST_LABEL_WILDCARD = r"[A-Za-z0-9-]+"

#: Keycloak's built-in account-console audience. Never something a client should mint for.
_REFUSED_AUDIENCES = frozenset({"account"})

#: Hosts that are always the developer's own machine. urlsplit gives IPv6 without brackets.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

CLIENT_ID_MAX_LENGTH = 255


def validate_redirect_uri(value: str, *, allow_wildcard: bool) -> str:
    """Check the structural shape of a redirect URI and return it stripped.

    Absolute http(s), a host, no credentials, no query and no fragment: the broker
    appends its own ``code`` and ``state``. With *allow_wildcard*, the first host label
    may contain `*` (e.g. ``https://pr-*-riad.d.alloy.ch/nannos-auth-callback.html``);
    nowhere else may. Whether plain http is allowed is an environment question the
    caller answers (see ``require_https_unless_local``).
    """
    candidate = (value or "").strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"redirect URI must start with https:// or http://: {candidate!r}"
        )
    if not parts.hostname or "@" in parts.netloc:
        raise ValueError(
            f"redirect URI must name a host and carry no credentials: {candidate!r}"
        )
    if parts.query or parts.fragment or candidate.endswith(("?", "#")):
        raise ValueError(
            f"redirect URI must not carry a query or fragment: {candidate!r}"
        )
    if "*" in candidate:
        if not allow_wildcard:
            raise ValueError(f"redirect URI must not contain '*': {candidate!r}")
        host = parts.netloc.split(":", 1)[0]
        first_label, _, rest = host.partition(".")
        if "*" in rest or "*" in parts.path or "*" in parts.netloc[len(host) :]:
            raise ValueError(
                f"'*' is only allowed in the first host label: {candidate!r}"
            )
        if not first_label.replace("*", "") or not rest:
            raise ValueError(
                f"a wildcard host label needs fixed characters and a parent domain: {candidate!r}"
            )
    return candidate


def redirect_uri_matches(registered: str, candidate: str) -> bool:
    """Whether *candidate* is allowed by the *registered* redirect URI.

    Exact string match, or — for a registered URI with `*` in its first host label —
    a full match where each `*` stands for DNS label characters.
    """
    if "*" not in registered:
        return candidate == registered
    pattern = "".join(
        _HOST_LABEL_WILDCARD if part == "*" else re.escape(part)
        for part in re.split(r"(\*)", registered)
    )
    return re.fullmatch(pattern, candidate) is not None


def _is_loopback(uri: str) -> bool:
    return urlsplit(uri).hostname in _LOOPBACK_HOSTS


def require_https_unless_local(
    redirect_uris: list[str], *, is_local: bool, allow_loopback: bool = False
) -> None:
    """Plain http is for local development only; everywhere else a redirect is https.

    With *allow_loopback* (dev and stg), plain http may also point at the developer's own
    machine, so a client running locally can sign in through a deployed broker. The code
    then only ever reaches that machine (RFC 8252 §7.3).
    """
    if is_local:
        return
    insecure = [
        uri
        for uri in redirect_uris
        if not uri.startswith("https://") and not (allow_loopback and _is_loopback(uri))
    ]
    if insecure:
        hint = " (plain http is allowed for localhost only)" if allow_loopback else ""
        raise ValueError(
            "redirect URIs must use https:// outside local development"
            + hint
            + ": "
            + ", ".join(insecure)
        )


def _clean_redirect_uris(values: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in values:
        uri = validate_redirect_uri(raw, allow_wildcard=True)
        if uri not in seen:
            seen.append(uri)
    return seen


class BrokerClientCreate(BaseModel):
    """Admin request body for registering a broker client."""

    client_id: str = Field(
        min_length=1,
        max_length=CLIENT_ID_MAX_LENGTH,
        description="Keycloak client id; the `azp` of the client's own client-credentials token.",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    redirect_uris: list[str] = Field(
        min_length=1,
        description=(
            "Where the broker may send the browser back to. Exact match; the first host label may "
            "contain '*' for per-PR preview environments."
        ),
    )
    enabled: bool = True

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("client_id must not be blank")
        return cleaned

    @field_validator("redirect_uris")
    @classmethod
    def _check_redirect_uris(cls, value: list[str]) -> list[str]:
        return _clean_redirect_uris(value)


class BrokerClientUpdate(BaseModel):
    """Admin request body for changing a broker client. Omitted fields are unchanged;
    ``description`` is the one field a null clears."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    redirect_uris: list[str] | None = Field(default=None, min_length=1)
    enabled: bool | None = None

    @field_validator("redirect_uris")
    @classmethod
    def _check_redirect_uris(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_redirect_uris(value)


class BrokerClient(BaseModel):
    """A registered broker client as the API returns it."""

    id: int
    client_id: str
    name: str
    description: str | None = None
    redirect_uris: list[str]
    enabled: bool
    created_by: str
    created_at: datetime
    updated_at: datetime

    def allows_redirect_uri(self, candidate: str) -> bool:
        return any(
            redirect_uri_matches(registered, candidate)
            for registered in self.redirect_uris
        )

    def may_mint_for(self, audience: str, always_granted: list[str]) -> bool:
        """Whether tokens for *audience* may be minted for this client: the audiences every
        client may, and its own client id, the audience an embedded host is bound by
        (ADR-0011 point 5). Never another client's id, which would let this client act as
        that host."""
        if audience in _REFUSED_AUDIENCES:
            return False
        return audience == self.client_id or audience in always_granted


class BrokerClientListResponse(BaseModel):
    clients: list[BrokerClient]
    always_granted_audiences: list[str] = Field(
        description="Audiences every client may have tokens minted for, in addition to its own client id."
    )


class BrokerIdentity(BaseModel):
    """Who signed in, as ``/redeem`` returns it.

    The union of the claims any broker client reads today, captured from the ID token
    (or userinfo) at sign-in. A client that needs fresh values signs the user in again.
    """

    user_id: str = Field(description="The console user id.")
    sub: str
    email: str | None = None
    email_verified: bool | None = None
    name: str | None = None
    preferred_username: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    groups: list[str] = Field(default_factory=list)
    phone_number: str | None = None
    phone_number_idp: str | None = None
    company_name: str | None = None


class BrokerRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)


class BrokerTokenRequest(BaseModel):
    sub: str = Field(
        min_length=1,
        max_length=255,
        description="The user's OIDC subject, from /redeem.",
    )
    audience: str = Field(min_length=1, max_length=255)


class BrokerTokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "Bearer"
