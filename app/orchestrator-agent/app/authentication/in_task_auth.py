"""
In-task authentication models for downstream service authentication.

These models provide a structured convention for communicating authentication
requirements when an A2A task encounters auth needs for third-party services
(e.g., GitHub OAuth, HR system CIBA approval, device code flows).

This is SEPARATE from A2A server-to-server authentication:
- SmartTokenInterceptor: Handles Orchestrator → Sub-Agent auth (A2A layer)
- These models: Handle Sub-Agent → Third-Party Service auth (in-task layer)

The A2A protocol provides TaskState.auth_required and DataPart for structured
data, but doesn't define the schema for auth payloads. These models establish
a reusable convention that can be adopted across A2A implementations.

Example Flow:
1. Orchestrator calls JIRA Agent (A2A auth via SmartTokenInterceptor)
2. JIRA Agent needs GitHub OAuth to fetch PR data (in-task auth)
3. JIRA Agent returns TaskState.auth_required with AuthPayload in DataPart
4. Orchestrator parses AuthPayload and prompts user for GitHub auth
5. User completes OAuth, orchestrator retries with credentials
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AuthenticationMethod(BaseModel):
    """
    Represents a specific authentication method for a downstream service.

    Supports multiple enterprise authentication patterns:
    - oauth2: Standard OAuth2 authorization code flow
    - ciba: Client-Initiated Backchannel Authentication (push notifications)
    - bearer_token: Simple bearer token authentication
    - api_key: API key-based authentication
    - saml: SAML-based single sign-on
    - device_code: OAuth2 device authorization flow

    Attributes:
        method: Type of authentication method
        description: Human-readable description of the auth requirement
        auth_url: URL to initiate authentication flow (for oauth2, ciba, device_code)
        instructions: User-facing instructions for completing auth
    """

    method: Literal["oauth2", "ciba", "bearer_token", "api_key", "saml", "device_code"]
    description: str
    auth_url: Optional[str] = None
    instructions: Optional[str] = None


class ServiceAuthRequirement(BaseModel):
    """
    Structured authentication requirement for a specific downstream service.

    Describes what authentication is needed, including the service name,
    supported auth methods, required scopes, and additional context.

    Attributes:
        service: Name of the service requiring auth (e.g., "github", "hr_api")
        resource: Optional specific resource within the service
        auth_methods: List of supported authentication methods (try in order)
        required_scopes: List of all required scopes across all methods
        token_type: Type of token (default: "Bearer")
        expires_in: Optional token expiration in seconds
    """

    service: str
    resource: Optional[str] = None
    auth_methods: List[AuthenticationMethod]
    required_scopes: List[str] = Field(default_factory=list)
    token_type: Optional[str] = "Bearer"
    expires_in: Optional[int] = None


class OAuth2ClientConfig(BaseModel):
    """
    OAuth2 client configuration for service authentication.

    Contains the OAuth2 client credentials needed to authenticate
    with a specific service. Scopes and audience are defined in
    the ServiceAuthRequirement to avoid duplication.

    Attributes:
        issuer: OAuth2 issuer URL (e.g., "https://company.okta.com")
        client_id: OAuth2 client ID for the service
        client_secret: OAuth2 client secret (kept secure)
        auth_method: OAuth2 grant type to use
    """

    issuer: str
    client_id: str
    client_secret: SecretStr
    auth_method: Literal["client_credentials", "jwt_bearer", "device_code"] = "client_credentials"


class AuthPayload(BaseModel):
    """
    Complete authentication payload following CIBA-inspired patterns.

    This is the top-level model that gets embedded in A2A Task responses
    when TaskState.auth_required. It provides all information needed for
    the client to present auth options and complete the flow.

    The payload should be sent in a DataPart within the Task's status message.

    Attributes:
        requires_auth: Always True (indicates auth is needed)
        auth_requirement: Details about what service needs auth and how
        oauth2_client_config: Optional OAuth2 client configuration
        session_id: Optional session identifier for tracking
        correlation_id: Optional correlation ID (often the message_id)

    Example:
        {
            "requires_auth": true,
            "auth_requirement": {
                "service": "github",
                "auth_methods": [{
                    "method": "oauth2",
                    "auth_url": "https://github.com/login/oauth/authorize?...",
                    "scopes": ["repo", "user:email"]
                }]
            }
        }
    """

    requires_auth: bool = True
    auth_requirement: ServiceAuthRequirement
    oauth2_client_config: Optional[OAuth2ClientConfig] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "requires_auth": True,
                "auth_requirement": {
                    "service": "github",
                    "resource": "repositories",
                    "auth_methods": [
                        {
                            "method": "oauth2",
                            "description": "GitHub OAuth for repository access",
                            "auth_url": "https://github.com/login/oauth/authorize",
                            "instructions": "Please authorize access to your GitHub repositories",
                        }
                    ],
                    "required_scopes": ["repo", "user:email"],
                },
            }
        }
    )


__all__ = [
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
