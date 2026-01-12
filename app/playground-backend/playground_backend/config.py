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
        default_factory=lambda: os.getenv("DYNAMODB_USERS_TABLE", "dev-alloy-infrastructure-agents-users")
    )
    sessions_table: str = Field(
        default_factory=lambda: os.getenv("DYNAMODB_SESSIONS_TABLE", "dev-alloy-infrastructure-agents-chat-ui-sessions")
    )
    conversations_table: str = Field(
        default_factory=lambda: os.getenv(
            "DYNAMODB_UI_CONVERSATIONS_TABLE", "dev-alloy-infrastructure-agents-chat-ui-conversations"
        )
    )
    messages_table: str = Field(
        default_factory=lambda: os.getenv(
            "DYNAMODB_UI_MESSAGES_TABLE", "dev-alloy-infrastructure-agents-chat-ui-messages"
        )
    )


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

    def is_local(self) -> bool:
        return self.environment == "local"

    def is_dev(self) -> bool:
        return self.environment in ("dev", "stg")

    def is_production(self) -> bool:
        return self.environment == "prod"


# Global config instance
config = Config()
