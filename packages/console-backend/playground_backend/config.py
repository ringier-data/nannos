"""Configuration for the A2A Inspector application."""

import os

from pydantic import BaseModel, Field, SecretStr


class OidcConfig(BaseModel):
    """OIDC configuration."""

    client_id: str = Field(default_factory=lambda: os.getenv("OIDC_CLIENT_ID", ""))
    client_secret: SecretStr = Field(default_factory=lambda: SecretStr(os.getenv("OIDC_CLIENT_SECRET", "")))
    issuer: str = Field(default_factory=lambda: os.environ["OIDC_ISSUER"])
    scope: str = Field(default="openid profile email offline_access")


class DynamoDBConfig(BaseModel):
    """DynamoDB configuration."""

    region: str = Field(default_factory=lambda: os.getenv("AWS_REGION", "eu-central-1"))
    users_table: str = Field(
        default_factory=lambda: os.getenv("DYNAMODB_USERS_TABLE", "dev-nannos-infrastructure-agents-users")
    )
    sessions_table: str = Field(
        default_factory=lambda: os.getenv(
            "DYNAMODB_SESSIONS_TABLE", "dev-nannos-infrastructure-agents-chat-ui-sessions"
        )
    )
    conversations_table: str = Field(
        default_factory=lambda: os.getenv(
            "DYNAMODB_CONVERSATIONS_TABLE", "dev-nannos-infrastructure-agents-chat-ui-conversations"
        )
    )
    messages_table: str = Field(
        default_factory=lambda: os.getenv(
            "DYNAMODB_MESSAGES_TABLE", "dev-nannos-infrastructure-agents-chat-ui-messages"
        )
    )


class FileStorageConfig(BaseModel):
    """S3 configuration for user-uploaded files including audio recordings."""

    bucket: str = Field(default_factory=lambda: os.getenv("FILES_S3_BUCKET", "dev-nannos-infrastructure-agents-files"))
    presigned_ttl_seconds: int = Field(default_factory=lambda: int(os.getenv("FILES_PRESIGNED_TTL_SECONDS", "3600")))
    prefix: str = Field(default_factory=lambda: os.getenv("FILES_S3_PREFIX", ""))


class PostgresConfig(BaseModel):
    """PostgreSQL database configuration."""

    host: str = Field(default_factory=lambda: os.getenv("POSTGRES_HOST", "localhost"))
    port: int = Field(default_factory=lambda: int(os.getenv("POSTGRES_PORT", "5432")))
    database: str = Field(default_factory=lambda: os.getenv("POSTGRES_DB", "playground"))
    user: str = Field(default_factory=lambda: os.getenv("POSTGRES_USER", "postgres"))
    password: SecretStr = Field(default_factory=lambda: SecretStr(os.getenv("POSTGRES_PASSWORD", "password")))
    default_schema: str = Field(default_factory=lambda: os.getenv("POSTGRES_SCHEMA", "playground"))

    @property
    def connection_url(self) -> str:
        """Build async PostgreSQL connection URL for SQLAlchemy."""
        return f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}@{self.host}:{self.port}/{self.database}"

    @property
    def sync_connection_url(self) -> str:
        """Build sync PostgreSQL connection URL for SQLAlchemy."""
        return f"postgresql://{self.user}:{self.password.get_secret_value()}@{self.host}:{self.port}/{self.database}"


class OrchestratorConfig(BaseModel):
    """Orchestrator agent configuration for token exchange."""

    client_id: str = Field(default_factory=lambda: os.getenv("ORCHESTRATOR_CLIENT_ID", ""))
    base_domain: str = Field(default_factory=lambda: os.getenv("ORCHESTRATOR_BASE_DOMAIN", ""))
    environment: str = Field(
        default_factory=lambda: os.getenv("ORCHESTRATOR_ENVIRONMENT", os.getenv("ENVIRONMENT", "local"))
    )

    def is_local(self) -> bool:
        """Check if orchestrator is running locally."""
        return self.environment == "local"

    def is_dev(self) -> bool:
        """Check if orchestrator is in dev/staging environment."""
        return self.environment in ("dev", "stg")

    def is_production(self) -> bool:
        """Check if orchestrator is in production environment."""
        return self.environment == "prod"


class KeycloakAdminConfig(BaseModel):
    """Keycloak Admin API configuration for group synchronization."""

    admin_client_id: str = Field(default_factory=lambda: os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", ""))
    admin_client_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", ""))
    )
    group_name_prefix: str = Field(default_factory=lambda: os.getenv("KEYCLOAK_GROUP_NAME_PREFIX", ""))


class MCPGatewayConfig(BaseModel):
    """MCP Gateway configuration for tool discovery."""

    url: str = Field(default_factory=lambda: os.getenv("MCP_GATEWAY_URL", "https://alloych.gatana.ai/mcp"))
    client_id: str = Field(default_factory=lambda: os.getenv("MCP_GATEWAY_CLIENT_ID", "gatana"))


class SchedulerConfig(BaseModel):
    """Scheduler engine configuration."""

    tick_interval_seconds: int = Field(default_factory=lambda: int(os.getenv("SCHEDULER_TICK_INTERVAL_SECONDS", "30")))
    claim_limit: int = Field(default_factory=lambda: int(os.getenv("SCHEDULER_CLAIM_LIMIT", "10")))
    agent_runner_url: str = Field(default_factory=lambda: os.getenv("AGENT_RUNNER_URL", "http://localhost:5005"))
    ai_model_id: str = Field(
        default_factory=lambda: os.getenv("SCHEDULER_AI_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    )  # NOTE: supports just bedrock models for now, but can be extended in the future


class AutoApproveConfig(BaseModel):
    """Auto-approve constraints for sub-agents."""

    max_system_prompt_length: int = Field(
        default_factory=lambda: int(os.getenv("AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH", "500"))
    )
    max_mcp_tools_count: int = Field(default_factory=lambda: int(os.getenv("AUTO_APPROVE_MAX_MCP_TOOLS_COUNT", "3")))


class Config(BaseModel):
    """Application configuration."""

    environment: str = Field(default_factory=lambda: os.getenv("ENVIRONMENT", "local"))
    base_domain: str = Field(default_factory=lambda: os.getenv("BASE_DOMAIN", "localhost:5001"))
    secret_key: str = Field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production"))
    session_ttl_seconds: int = Field(default=2592000)  # 30 days
    cookie_name: str = Field(default="a2a-chatui")

    oidc: OidcConfig = Field(default_factory=OidcConfig)
    dynamodb: DynamoDBConfig = Field(default_factory=DynamoDBConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    keycloak_admin: KeycloakAdminConfig = Field(default_factory=KeycloakAdminConfig)
    mcp_gateway: MCPGatewayConfig = Field(default_factory=MCPGatewayConfig)
    file_storage: FileStorageConfig = Field(default_factory=FileStorageConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    auto_approve: AutoApproveConfig = Field(default_factory=AutoApproveConfig)

    def is_local(self) -> bool:
        return self.environment == "local"

    def is_dev(self) -> bool:
        return self.environment in ("dev", "stg")

    def is_production(self) -> bool:
        return self.environment == "prod"


# Global config instance
config = Config()
