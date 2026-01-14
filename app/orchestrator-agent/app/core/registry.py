"""Registry service for fetching approved sub-agents from the playground backend."""

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..a2a_utils.models import LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig, LocalSubAgentConfig

logger = logging.getLogger(__name__)

# System prompt addendum for playground mode
PLAYGROUND_MODE_ADDENDUM = """

**PLAYGROUND MODE - SINGLE SUB-AGENT TESTING**
You are in playground testing mode. You MUST follow these rules strictly:
1. You have access to ONLY ONE sub-agent: "{subagent_name}"
2. You MUST delegate ALL user requests to this sub-agent
3. Do NOT attempt to handle requests yourself - always use the sub-agent
4. Do NOT use any other sub-agents or tools
5. If the sub-agent cannot handle a request, explain what the sub-agent's capabilities are

This mode is used for testing and validating sub-agent behavior in isolation.
"""


class SubAgentConfigVersion(BaseModel):
    """Configuration version data embedded in SubAgent response."""

    id: int | None = None
    sub_agent_id: int | None = None
    version: int
    description: str | None = None  # Agent skill set description - crucial for orchestrator routing
    model: str | None = None  # LLM model to use (e.g., 'gpt-4', 'claude-3-opus')

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url
    system_prompt: str | None = None  # For local sub-agents: the system prompt
    agent_url: str | None = None  # For remote sub-agents: the URL of the agent
    mcp_tools: list[str] = []  # MCP tool names enabled for this version

    # Foundry agent configuration
    foundry_hostname: str | None = None
    foundry_client_id: str | None = None
    foundry_client_secret_ssmkey: str | None = None
    foundry_ontology_rid: str | None = None
    foundry_query_api_name: str | None = None
    foundry_scopes: list[str] | None = None  # Stored as TEXT[] in database
    foundry_version: str | None = None

    change_summary: str | None = None
    status: str
    approved_by_user_id: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime


class SubAgent(BaseModel):
    """Sub-agent model matching the normalized backend response.

    Metadata (name, owner, type) comes from sub_agents table.
    Configuration data comes from the embedded config_version.
    """

    id: int
    name: str
    owner_user_id: str
    type: str
    current_version: int = 1
    default_version: int | None = None
    config_version: SubAgentConfigVersion | None = None  # Embedded version data
    created_at: datetime
    updated_at: datetime


class RegistryConfig(BaseModel):
    """Configuration for the registry service."""

    playground_backend_url: str = Field(
        default_factory=lambda: os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
    )


class User(BaseModel):
    """User model with sub-agents fetched from the registry.

    Note: This model is maintained for backward compatibility but now
    focuses on sub-agents rather than the deprecated DynamoDB-based approach.
    """

    id: str  # Primary key (sub from OIDC)
    agent_metadata: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )  # Maps agent_url -> {sub_agent_id, name, description}
    tool_names: list[str] = Field(default_factory=list)  # MCP tool names enabled for orchestrator
    language: str = "en"  # User's preferred language
    custom_prompt: str | None = None  # User's custom prompt addendum
    local_subagents: list[LocalSubAgentConfig] = Field(default_factory=list)  # Local sub-agents
    sub_agent_config_hash: str | None = None  # Playground mode: version hash for testing
    playground_subagent_name: str | None = None  # Playground mode: name for system prompt


class RegistryService:
    """Service for fetching approved sub-agents from the playground backend.

    This service calls the /api/v1/sub-agents endpoint to retrieve all approved
    sub-agents accessible to the user (owned + group-shared).

    The access token is passed in the Authorization header to authenticate
    the request and determine which sub-agents the user can access.
    """

    def __init__(self, config: RegistryConfig | None = None) -> None:
        """Initialize the registry service.

        Args:
            config: Optional configuration. If None, uses environment defaults.
        """
        self.config = config or RegistryConfig()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.playground_backend_url,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_sub_agents(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: str,
        sub_agent_config_hash: str | None,
    ) -> list[SubAgent]:
        """Fetch sub-agents from the backend.

        In playground mode, fetches a specific config version by hash.
        Otherwise, fetches all approved sub-agents with their default versions.
        """
        # Playground mode: fetch only the specific config version for testing
        if sub_agent_config_hash is not None:
            response = await client.get(
                f"/api/v1/sub-agents/configs/by-hash/{sub_agent_config_hash}",
                headers=headers,
            )
            if response.status_code == 401:
                logger.warning(f"Authentication failed for playground config hash {sub_agent_config_hash}")
                return []
            if response.status_code == 403:
                logger.warning(f"Access denied for playground config hash {sub_agent_config_hash}")
                return []
            if response.status_code == 404:
                logger.warning(f"Playground config hash {sub_agent_config_hash} not found")
                return []
            if response.status_code != 200:
                logger.error(
                    f"Failed to fetch playground config hash {sub_agent_config_hash}: "
                    f"status={response.status_code}, body={response.text}"
                )
                return []
            data = response.json()
            return [SubAgent.model_validate(data)] if data else []
        else:
            # Normal mode: fetch all approved sub-agents (those with default_version set)
            # Only fetch activated sub-agents for this user
            response = await client.get(
                "/api/v1/sub-agents",
                params={"status": "approved", "activated_only": True},
                headers=headers,
            )

            if response.status_code == 401:
                logger.warning(f"Authentication failed for user {user_id}")
                return []

            if response.status_code != 200:
                logger.error(
                    f"Failed to fetch sub-agents for user {user_id}: "
                    f"status={response.status_code}, body={response.text}"
                )
                return []

            data = response.json()
            sub_agents = []
            for item in data.get("items", []):
                # Only include sub-agents that have an approved default version
                if item.get("default_version") is not None and item.get("config_version"):
                    sub_agents.append(SubAgent.model_validate(item))
            return sub_agents

    async def get_user(
        self, user_id: str, access_token: str | None = None, sub_agent_config_hash: str | None = None
    ) -> User | None:
        """Retrieve approved sub-agents for a user from the playground backend.

        Calls GET /api/v1/sub-agents?status=approved to fetch all approved
        sub-agents accessible to the user (owned + group-shared).

        When sub_agent_config_hash is provided, fetches only that specific
        config version for isolated testing (playground mode).

        Args:
            user_id: The user's ID (sub from OIDC)
            access_token: The user's access token for authentication
            sub_agent_config_hash: Optional config version hash for playground mode testing

        Returns:
            User object with populated agent_metadata and local_subagents, or None on error
        """
        if not access_token:
            logger.warning(f"No access token provided for user {user_id}, returning empty user")
            return User(id=user_id)

        try:
            client = await self._get_client()
            headers = {"Authorization": f"Bearer {access_token}"}

            sub_agents = await self.get_sub_agents(
                client,
                headers,
                user_id,
                sub_agent_config_hash,
            )

            # Fetch user settings for language and custom_prompt
            settings = await self._fetch_user_settings(client, headers, user_id)

            # Convert to User model format with settings
            user = self._to_user(user_id, sub_agents, settings)

            # Add playground mode info if applicable
            if sub_agent_config_hash is not None and sub_agents:
                sub_agent = sub_agents[0]
                user.sub_agent_config_hash = sub_agent_config_hash
                user.playground_subagent_name = sub_agent.name

                # Add playground mode addendum to custom prompt
                playground_addendum = PLAYGROUND_MODE_ADDENDUM.format(subagent_name=sub_agent.name)
                base_custom_prompt = settings.get("custom_prompt") or ""
                user.custom_prompt = base_custom_prompt + playground_addendum

                logger.info(
                    f"Playground mode: loaded sub-agent '{sub_agent.name}' "
                    f"(config hash: {sub_agent_config_hash}) for user {user_id}"
                )

            return user

        except httpx.TimeoutException:
            logger.error(f"Timeout fetching sub-agents for user {user_id}")
            return User(id=user_id)
        except httpx.RequestError as e:
            logger.error(f"Request error fetching sub-agents for user {user_id}: {e}")
            return User(id=user_id)
        except Exception as e:
            logger.error(f"Unexpected error fetching sub-agents for user {user_id}: {e}")
            return None

    async def _fetch_user_settings(
        self, client: httpx.AsyncClient, headers: dict[str, str], user_id: str
    ) -> dict[str, Any]:
        """Fetch user settings from the playground backend.

        Args:
            client: The HTTP client
            headers: Request headers with Authorization
            user_id: The user's ID for logging

        Returns:
            Dictionary with 'language', 'custom_prompt', and 'mcp_tools' keys, or defaults
        """
        default_settings = {"language": "en", "timezone": "Europe/Zurich", "custom_prompt": None, "mcp_tools": []}

        try:
            response = await client.get("/api/v1/auth/me/settings", headers=headers)

            if response.status_code != 200:
                logger.warning(f"Failed to fetch settings for user {user_id}: status={response.status_code}")
                return default_settings

            data = response.json()
            settings_data = data.get("data", {})

            return {
                "language": settings_data.get("language", "en"),
                "timezone": settings_data.get("timezone", "Europe/Zurich"),
                "custom_prompt": settings_data.get("custom_prompt"),
                "mcp_tools": settings_data.get("mcp_tools", []),
            }

        except Exception as e:
            logger.warning(f"Error fetching settings for user {user_id}: {e}")
            return default_settings

    def _to_user(self, user_id: str, sub_agents: list[SubAgent], settings: dict[str, Any]) -> User:
        """Convert sub-agents from the backend response to User model format.

        Args:
            user_id: The user's ID
            sub_agents: List of SubAgent objects from the backend
            settings: User settings with 'language' and 'custom_prompt' keys

        Returns:
            User object with agent_metadata, local_subagents, language, and custom_prompt
        """
        agent_metadata: dict[str, dict[str, Any]] = {}  # Maps agent_url -> {sub_agent_id, name, etc.}
        local_subagents: list[LocalSubAgentConfig] = []

        for sa in sub_agents:
            logger.debug(f"Processing sub-agent '{sa.name}' of type '{sa.type}' for user {user_id}")
            if not sa.config_version:
                continue

            cv = sa.config_version
            if sa.type == "remote":
                # Remote A2A agents have agent_url at root level
                agent_url = cv.agent_url
                if agent_url:
                    # Store metadata for remote agents (sub_agent_id for cost tracking)
                    agent_metadata[agent_url] = {
                        "sub_agent_id": sa.id,
                        "name": sa.name,
                        "description": cv.description,
                    }
            elif sa.type == "local":
                # Local agents have system_prompt and mcp_tools at root level
                system_prompt = cv.system_prompt or ""
                mcp_tools = cv.mcp_tools or []

                if sa.name and system_prompt:
                    local_subagents.append(
                        LocalLangGraphSubAgentConfig(
                            name=sa.name,
                            description=cv.description or f"Local agent: {sa.name}",
                            system_prompt=system_prompt,
                            mcp_tools=mcp_tools if mcp_tools else None,
                            model_name=cv.model,
                            sub_agent_id=sa.id,  # Include playground backend ID for tracking
                        )
                    )
                    logger.debug(f"Added local sub-agent '{local_subagents[-1].model_dump_json()}' for user {user_id}")
            elif sa.type == "foundry":
                # Foundry agents have foundry-specific configuration fields
                if sa.name and cv.foundry_hostname and cv.foundry_query_api_name:
                    local_subagents.append(
                        LocalFoundrySubAgentConfig(
                            name=sa.name,
                            description=cv.description or f"Foundry agent: {sa.name}",
                            sub_agent_id=cv.sub_agent_id,  # Include playground backend ID for tracking
                            hostname=cv.foundry_hostname,
                            client_id=cv.foundry_client_id or "",
                            client_secret_ref=cv.foundry_client_secret_ssmkey or "",
                            ontology_rid=cv.foundry_ontology_rid or "",
                            query_api_name=cv.foundry_query_api_name,
                            scopes=cv.foundry_scopes or [],
                            version=cv.foundry_version,
                        )
                    )
                    logger.debug(
                        f"Added Foundry local sub-agent '{local_subagents[-1].model_dump_json()}' for user {user_id}"
                    )
        logger.debug(
            f"Converted sub-agents for user {user_id}: {len(agent_metadata)} remote, {len(local_subagents)} local"
        )

        return User(
            id=user_id,
            agent_metadata=agent_metadata,  # Include metadata for remote agents
            tool_names=settings.get("mcp_tools", []),  # MCP tools from user settings
            local_subagents=local_subagents,
            language=settings.get("language", "en"),
            custom_prompt=settings.get("custom_prompt"),
        )
