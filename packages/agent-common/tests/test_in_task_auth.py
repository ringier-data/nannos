"""In-task auth payloads: what may cross to an end-user client, and what may not.

``AuthPayload`` serves two audiences. A SERVER completing a flow on the user's
behalf may need ``oauth2_client_config`` — which carries a client secret. A
browser being asked to authorize never does. ``client_payload()`` is the
boundary between them, and these tests are what keep it one.
"""

from agent_common.a2a.authentication import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
)


def test_client_payload_omits_oauth2_client_config():
    """The secret-bearing half never reaches the wire, even when it is set."""
    payload = AuthPayload(
        auth_requirement=ServiceAuthRequirement(
            service="github",
            auth_methods=[AuthenticationMethod(method="oauth2", description="GitHub OAuth")],
        ),
        oauth2_client_config=OAuth2ClientConfig(
            issuer="https://issuer.example",
            client_id="client-id",
            client_secret="super-secret-value",
        ),
    )

    client = payload.client_payload()

    assert "oauth2_client_config" not in client
    assert "super-secret-value" not in repr(client)
    assert "client_secret" not in repr(client)


def test_client_payload_is_built_from_the_requirement_not_a_filtered_dump():
    """A key added to the model later must not appear here by default.

    ``client_payload()`` names the fields it emits rather than dumping the model
    and deleting the dangerous ones — so an unknown field is excluded by
    construction, which is the property worth pinning.
    """
    payload = AuthPayload.for_service("github", auth_url="https://gatana.example/begin")
    object.__setattr__(payload, "__dict__", {**payload.__dict__, "future_secret": "leak"})

    assert set(payload.client_payload()) <= {
        "requires_auth",
        "auth_requirement",
        "session_id",
        "correlation_id",
    }


def test_for_service_carries_url_and_resource():
    payload = AuthPayload.for_service(
        "github",
        auth_url="https://gatana.example/begin",
        resource="github_get_me",
        correlation_id="call-1",
    )

    client = payload.client_payload()
    requirement = client["auth_requirement"]

    assert client["requires_auth"] is True
    assert client["correlation_id"] == "call-1"
    assert requirement["service"] == "github"
    assert requirement["resource"] == "github_get_me"
    assert requirement["auth_methods"][0]["auth_url"] == "https://gatana.example/begin"
    assert requirement["auth_methods"][0]["method"] == "oauth2"


def test_for_service_omits_empty_optionals():
    """No URL and no correlation id → absent keys, not empty strings.

    A client branching on "is there a URL" must not have to treat "" as absent.
    """
    client = AuthPayload.for_service("github").client_payload()

    assert "correlation_id" not in client
    assert "resource" not in client["auth_requirement"]
    assert "auth_url" not in client["auth_requirement"]["auth_methods"][0]
