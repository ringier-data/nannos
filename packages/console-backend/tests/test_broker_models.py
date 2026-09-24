"""Token broker models: which redirect URIs a client may register, and which requests
they allow. The broker sends a one-time code to whatever redirect URI it accepts, so
these rules are what keeps it from being an open redirect."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from console_backend.models.broker import (
    BrokerClient,
    BrokerClientCreate,
    BrokerClientUpdate,
    redirect_uri_matches,
    require_https_unless_local,
    validate_redirect_uri,
)


def _create(**overrides) -> BrokerClientCreate:
    fields = {
        "client_id": "slack-client",
        "name": "Slack",
        "redirect_uris": ["https://slack.nannos.ringier.ch/api/v1/oauth/callback"],
    }
    return BrokerClientCreate(**(fields | overrides))


class TestValidateRedirectUri:
    @pytest.mark.parametrize(
        "uri",
        [
            "https://slack.nannos.ringier.ch/api/v1/oauth/callback",
            "http://localhost:3600/api/v1/oauth/callback",
            "https://riad.alloy.ch/nannos-auth-callback.html",
        ],
    )
    def test_accepts_absolute_http_uris(self, uri):
        assert validate_redirect_uri(uri, allow_wildcard=False) == uri

    @pytest.mark.parametrize(
        "uri",
        [
            "slack://open?team=T1",  # not http(s)
            "https:///no-host",
            "https://user:pw@slack.nannos.ringier.ch/cb",  # credentials
            "https://evil.example@slack.nannos.ringier.ch/cb",
            "https://slack.nannos.ringier.ch/cb?next=/x",  # the broker appends its own query
            "https://slack.nannos.ringier.ch/cb#frag",
            "https://slack.nannos.ringier.ch/cb?",
            "/relative/cb",
        ],
    )
    def test_rejects_unsafe_shapes(self, uri):
        with pytest.raises(ValueError):
            validate_redirect_uri(uri, allow_wildcard=True)

    def test_a_request_may_never_carry_a_wildcard(self):
        with pytest.raises(ValueError):
            validate_redirect_uri(
                "https://pr-*-riad.d.alloy.ch/cb", allow_wildcard=False
            )

    def test_wildcard_only_in_the_first_host_label(self):
        assert validate_redirect_uri(
            "https://pr-*-riad.d.alloy.ch/cb", allow_wildcard=True
        )
        for uri in (
            "https://pr-1.*.alloy.ch/cb",  # not the first label
            "https://riad.d.alloy.ch/*",  # path
            "https://riad.d.alloy.ch:*/cb",  # port
            "https://*/cb",  # no parent domain
            "https://*.alloy.ch/cb",  # no fixed characters in the label
        ):
            with pytest.raises(ValueError):
                validate_redirect_uri(uri, allow_wildcard=True)


class TestRedirectUriMatches:
    def test_exact_match_is_a_plain_string_comparison(self):
        registered = "https://slack.nannos.ringier.ch/api/v1/oauth/callback"
        assert redirect_uri_matches(registered, registered)
        assert not redirect_uri_matches(registered, registered + "/")
        assert not redirect_uri_matches(
            registered, "https://slack.nannos.ringier.ch/api/v1/oauth/callback2"
        )

    def test_wildcard_stands_for_dns_label_characters_only(self):
        registered = "https://pr-*-riad.d.alloy.ch/nannos-auth-callback.html"
        assert redirect_uri_matches(
            registered, "https://pr-123-riad.d.alloy.ch/nannos-auth-callback.html"
        )
        assert redirect_uri_matches(
            registered, "https://pr-1a2b-riad.d.alloy.ch/nannos-auth-callback.html"
        )
        # Anything that would move the host or reach into userinfo does not match.
        assert not redirect_uri_matches(
            registered, "https://pr-1.evil-riad.d.alloy.ch/nannos-auth-callback.html"
        )
        assert not redirect_uri_matches(
            registered,
            "https://pr-x@evil.com/-riad.d.alloy.ch/nannos-auth-callback.html",
        )
        assert not redirect_uri_matches(
            registered, "https://pr--riad.d.alloy.ch/nannos-auth-callback.html"
        )
        assert not redirect_uri_matches(
            registered, "https://pr-1-riad.d.alloy.ch/other.html"
        )


class TestBrokerClientCreate:
    def test_cleans_and_dedupes(self):
        body = _create(
            client_id="  slack-client ",
            redirect_uris=[
                " https://slack.nannos.ringier.ch/cb ",
                "https://slack.nannos.ringier.ch/cb",
            ],
        )
        assert body.client_id == "slack-client"
        assert body.redirect_uris == ["https://slack.nannos.ringier.ch/cb"]

    def test_needs_at_least_one_redirect_uri(self):
        with pytest.raises(ValidationError):
            _create(redirect_uris=[])

    def test_update_validates_what_it_is_given(self):
        assert BrokerClientUpdate(enabled=False).redirect_uris is None
        with pytest.raises(ValidationError):
            BrokerClientUpdate(redirect_uris=["https://x.example/cb?q=1"])


class TestMayMintFor:
    ALWAYS = ["orchestrator", "agent-console"]

    @staticmethod
    def _client(client_id: str) -> BrokerClient:
        now = datetime.now(timezone.utc)
        return BrokerClient(
            id=1,
            client_id=client_id,
            name=client_id,
            redirect_uris=["https://x.example/cb"],
            enabled=True,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )

    def test_the_always_granted_audiences_and_its_own_client_id(self):
        cockpit = self._client("cockpit-embed")
        assert cockpit.may_mint_for("orchestrator", self.ALWAYS)
        assert cockpit.may_mint_for("agent-console", self.ALWAYS)
        assert cockpit.may_mint_for("cockpit-embed", self.ALWAYS)

    def test_never_another_clients_id(self):
        # That audience would bind the token to the other client's embedded agent.
        assert not self._client("slack-client").may_mint_for("cockpit-embed", self.ALWAYS)
        assert not self._client("slack-client").may_mint_for("gatana", self.ALWAYS)

    def test_never_the_account_console(self):
        assert not self._client("account").may_mint_for("account", self.ALWAYS)
        assert not self._client("slack-client").may_mint_for("account", ["account"])


def test_plain_http_only_in_local_development():
    uris = ["http://localhost:3600/cb", "https://slack.nannos.ringier.ch/cb"]
    require_https_unless_local(uris, is_local=True)
    with pytest.raises(ValueError, match="http://localhost:3600/cb"):
        require_https_unless_local(uris, is_local=False)


def test_plain_http_to_loopback_when_allowed():
    loopback = [
        "http://localhost:3000/cb",
        "http://127.0.0.1:3000/cb",
        "http://[::1]:3000/cb",
    ]
    require_https_unless_local(loopback, is_local=False, allow_loopback=True)
    with pytest.raises(ValueError, match="http://localhost:3000/cb"):
        require_https_unless_local(loopback, is_local=False)
    # Only the host counts: a loopback name elsewhere in the URI does not make it loopback.
    for uri in [
        "http://localhost.evil.com/cb",
        "http://evil.com/localhost",
        "http://pr-*.d.alloy.ch/cb",
    ]:
        with pytest.raises(ValueError, match="localhost only"):
            require_https_unless_local([uri], is_local=False, allow_loopback=True)
