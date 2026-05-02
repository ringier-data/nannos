"""
Foundry Agent Runnable - Integrates Palantir Foundry ontology queries into the orchestrator.

This runnable allows the orchestrator to execute Foundry ontology queries as sub-agents.
It retrieves the client_secret from AWS SSM Parameter Store at runtime and uses the
Foundry SDK to execute queries.

Architecture:
- Fetches client_secret from SSM using the secret reference ID from the database
- Uses foundry-platform-sdk's AsyncFoundryClient for ontology queries
- Follows A2A protocol for response formatting
- Supports OAuth2 authentication with Foundry

Configuration Flow:
1. User creates Foundry sub-agent via console frontend
2. Client secret stored in SSM Parameter Store (via SecretsService)
3. Secret reference ID stored in sub_agent_config_versions.foundry_client_secret_ref
4. Orchestrator fetches secret at runtime when instantiating the agent
5. Agent uses secret for OAuth2 authentication with Foundry

Environment Variables Required:
- SSM_VAULT_PREFIX: Prefix for SSM parameter paths (e.g., "/nannos/infrastructure-agents/vault")
- AWS_REGION: AWS region for SSM (default: "eu-central-1")
"""

import logging
import os
from typing import Any, Literal, Optional

from a2a.types import TaskState
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from deepagents import CompiledSubAgent
from foundry_sdk import AsyncFoundryClient as PlatformAsyncFoundryClient
from foundry_sdk import Auth, ConfidentialClientAuth
from pydantic import BaseModel, Field, model_validator

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.models import LocalFoundrySubAgentConfig
from agent_common.a2a.stream_events import StreamEvent, TaskResponseData

logger = logging.getLogger(__name__)


class StructuredResponse(BaseModel):
    task_state: TaskState = Field(
        ...,
        description="The state of the task. In case approval or input is required, the state should be 'input_required'.",
    )
    message: str = Field(..., description="A human-readable message providing details about the task state.")

    @model_validator(mode="before")
    def validate_state(cls, values):
        # input_required -> input-required
        if "state" in values:
            state = values.pop("state")
        elif "task_state" in values:
            state = values.pop("task_state")
        else:
            return values
        if isinstance(state, str):
            values["task_state"] = state.replace("_", "-")
        return values


class FoundryLocalAgentRunnable(LocalA2ARunnable):
    """Runnable for executing Foundry ontology queries as sub-agents.

    TODO: shall we inherit from DynamicLocalAgentRunnable instead?
    TODO: shall we additionally wrap it in a langgraph agent?

    This agent:
    1. Retrieves the client_secret from SSM at runtime
    2. Authenticates with Foundry using OAuth2
    3. Executes the configured query API
    4. Returns results in A2A protocol format

    Example usage in orchestrator registry:
        ```python
        foundry_agent = FoundryAgentRunnable(
            config=FoundryAgentConfig(
                name="jira-ticket-creator",
                description="Creates Jira tickets in Foundry ontology",
                hostname="https://blumen.palantirfoundry.de",
                client_id="my-client-id",
                client_secret_ref="/nannos/infrastructure-agents/vault/foundry/jira-client-secret",
                ontology_rid="ri.ontology.main.ontology.xxx",
                query_api_name="a2ATicketWriterAgent",
                scopes=["api:use-ontologies-write"],
                version=None,
            ),
            user={"id": "123", "email": "user@example.com"}
        )
        ```
    """

    def __init__(
        self,
        config: LocalFoundrySubAgentConfig,
        user: dict[str, Any],
        backend_url: Optional[str] = None,
        sub_agent_id: Optional[int] = None,
    ):
        """Initialize Foundry agent runnable.

        Args:
            config: Foundry agent configuration
            user: User context dict
            backend_url: Backend URL for cost tracking (optional)
            sub_agent_id: Sub-agent ID for cost attribution (optional)
        """
        # Initialize mixins first
        super().__init__()

        self.config = config
        self.user = user
        self.sub_agent_id = sub_agent_id
        self._client: PlatformAsyncFoundryClient | None = None
        self._client_secret: str | None = None

        # AWS session for SSM
        self.session = get_session()
        self.region_name = os.environ.get("AWS_REGION", "eu-central-1")

        # Enable cost tracking if backend_url provided
        if backend_url:
            self.enable_cost_tracking(
                backend_url=backend_url,
                sub_agent_id=sub_agent_id,
            )
            logger.info(f"Cost tracking enabled for Foundry agent '{self.config.name}' (sub_agent_id={sub_agent_id})")
        else:
            logger.info(f"Cost tracking not enabled for Foundry agent '{self.config.name}' (no backend_url)")

    @property
    def name(self) -> str:
        """Return the agent name."""
        return self.config.name

    def get_supported_input_modes(self) -> list[str]:
        """Return the list of input modalities supported by this agent."""
        # Foundry agents are code execution agents, not multimodal
        return ["text"]

    @property
    def description(self) -> str:
        """Return the agent description."""
        return self.config.description

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Return checkpoint namespace for this agent.

        Args:
            input_data: Validated input data

        Returns:
            Checkpoint namespace (e.g., "foundry")
        """
        return "foundry"

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking.

        Args:
            input_data: Validated input data

        Returns:
            Sub-agent identifier (sub_agent_id if available, otherwise agent name)
        """
        if self.sub_agent_id is not None:
            return str(self.sub_agent_id)
        return f"foundry-{self.config.name}"

    async def _get_client_secret(self) -> str:
        """Retrieve client_secret from SSM Parameter Store.

        Returns:
            Decrypted client secret

        Raises:
            ValueError: If parameter not found or decryption fails
        """
        if self._client_secret is not None:
            return self._client_secret

        try:
            async with self.session.create_client("ssm", region_name=self.region_name) as ssm_client:
                response = await ssm_client.get_parameter(Name=self.config.client_secret_ref, WithDecryption=True)
                self._client_secret = response["Parameter"]["Value"]
                logger.info(f"Retrieved client_secret from SSM: {self.config.client_secret_ref}")
                return self._client_secret
        except ClientError as e:
            logger.error(f"Failed to get SSM parameter {self.config.client_secret_ref}: {e}")
            raise ValueError(f"Failed to retrieve client_secret from SSM: {e}")

    async def _get_foundry_client(self) -> PlatformAsyncFoundryClient:
        """Get or create Foundry client with OAuth2 authentication.

        Returns:
            Authenticated Foundry client

        Raises:
            ValueError: If client_secret retrieval fails
        """
        if self._client is not None:
            return self._client

        # Retrieve client_secret from SSM
        client_secret = await self._get_client_secret()

        # Create OAuth2 authentication
        auth: Auth = ConfidentialClientAuth(
            client_id=self.config.client_id,
            client_secret=client_secret,
            hostname=self.config.hostname,
            should_refresh=True,
            scopes=self.config.scopes,
        )

        # Create Foundry client
        self._client = PlatformAsyncFoundryClient(
            auth=auth,
            hostname=self.config.hostname,
            preview=True,  # Required for AsyncFoundryClient (beta)
        )

        logger.info(f"Created Foundry client for {self.config.hostname}")
        return self._client

    async def _execute_query(self, query_params: dict[str, Any]) -> dict[str, Any]:
        """Execute Foundry ontology query.

        Args:
            query_params: Parameters to pass to the query API

        Returns:
            Query execution result

        Raises:
            Exception: If query execution fails
        """
        client = await self._get_foundry_client()

        try:
            # Execute the query API using ontologies.Query.execute
            logger.info(f"Executing Foundry query: {self.config.query_api_name} with params: {query_params}")
            response = await client.ontologies.Query.execute(
                ontology=self.config.ontology_rid,
                query_api_name=self.config.query_api_name,
                parameters=query_params,
                version=self.config.version,
            )
            logger.info(f"Executed Foundry query: {self.config.query_api_name}")
            # Return the value, not the raw response
            return response.value
        except Exception as e:
            logger.error(f"Failed to execute Foundry query {self.config.query_api_name}: {e}")
            raise

    def _build_response_from_schema(
        self,
        schema: StructuredResponse,
        context_id: Optional[str],
        task_id: Optional[str],
        foundry_session_rid: Optional[str] = None,
    ) -> TaskResponseData:
        """Build A2A response from structured response schema.

        Args:
            schema: The structured response from the agent
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID (not used for Foundry session tracking)
            foundry_session_rid: Foundry session RID for conversation continuity

        Returns:
            TaskResponseData with typed lifecycle fields
        """
        if schema.task_state == "completed":
            return self._build_success_response(
                schema.message, context_id=context_id, task_id=task_id, foundry_session_rid=foundry_session_rid
            )
        elif schema.task_state == "input_required":
            return self._build_input_required_response(
                schema.message, context_id=context_id, task_id=task_id, foundry_session_rid=foundry_session_rid
            )
        else:  # failed
            return self._build_error_response(
                schema.message, context_id=context_id, task_id=task_id, foundry_session_rid=foundry_session_rid
            )

    async def _process(self, input_data: SubAgentInput, config: dict[str, Any]) -> TaskResponseData:
        """Execute the Foundry agent.

        Args:
            input_data: Validated input with messages and a2a_tracking
            config: Pre-configured RunnableConfig with checkpoint isolation and cost tracking

        Returns:
            TaskResponseData (plain, will be wrapped by base class)
        """
        try:
            # Extract content and tracking IDs using inherited helpers
            content = self._extract_message_content(input_data)
            context_id, task_id = self._extract_tracking_ids(input_data)

            # Retrieve stored foundry_session_rid from a2a_tracking state
            foundry_session_rid = None
            if input_data.a2a_tracking:
                agent_tracking = input_data.a2a_tracking.get(self.name, {})
                foundry_session_rid = agent_tracking.get("foundry_session_rid")
                if foundry_session_rid:
                    logger.debug(f"Reusing Foundry session: {foundry_session_rid}")

            query_params = {"userInput": content, "sessionRid": foundry_session_rid}

            # Execute Foundry query
            result = await self._execute_query(query_params)
            logger.info(f"Foundry agent execution result: {result}")

            # Extract sessionRid from response for persistence
            new_session_rid = result.get("sessionRid")
            if new_session_rid:
                logger.debug(f"Foundry session created: {new_session_rid}")

            # Extract and report usage for cost tracking
            logger.debug("Reporting Foundry agent usage for cost tracking")
            await self._report_usage(result, context_id)

            # Format response content
            try:
                task_id = result.get("sessionRid", task_id)
                structured_response = StructuredResponse.model_validate_json(result.get("markdownResponse", "{}"))
            except Exception:
                # TODO: let llm infer the task state
                structured_response = StructuredResponse(
                    task_state=self.classify(result.get("markdownResponse", "")),
                    message=result.get("markdownResponse", ""),
                )
            return self._build_response_from_schema(
                structured_response, context_id=context_id, task_id=task_id, foundry_session_rid=new_session_rid
            )

        except Exception as e:
            logger.error(f"Foundry agent execution failed: {e}", exc_info=True)

            # Return error response (task_id/context_id may be None if extraction failed)
            try:
                ctx_id, tsk_id = self._extract_tracking_ids(input_data)
            except Exception:
                ctx_id, tsk_id = None, None

            return self._build_error_response(f"Foundry agent execution failed: {e}", context_id=ctx_id, task_id=tsk_id)

    async def _report_usage(
        self,
        result: dict[str, Any],
        context_id: Optional[str],
    ) -> None:
        """Extract usage from Foundry response and report for cost tracking.

        Manual instrumentation: Checks for optional 'usage' field in response.
        If present, uses it for cost tracking. Otherwise defaults to counting requests.

        Args:
            result: Foundry API response
            context_id: Context ID for conversation tracking
        """
        if not self._cost_tracking_enabled:
            return

        # Extract usage from response if present
        usage_data = result.get("usage")

        if usage_data and isinstance(usage_data, dict):
            # Use the provided usage breakdown
            billing_unit_breakdown = usage_data
            logger.debug(f"Foundry agent usage from response: {billing_unit_breakdown}")
        else:
            # Default to counting requests
            billing_unit_breakdown = {"requests": 1}
            logger.debug("Foundry agent usage: defaulting to request count")

        # Report usage via cost tracking mixin
        # sub_agent_id is automatically used from CostLogger instance attribute
        logger.debug(f"Reporting Foundry agent usage: {billing_unit_breakdown}")
        await self.report_llm_usage(
            user_sub=self.user.get("sub", "unknown"),
            billing_unit_breakdown=billing_unit_breakdown,
            provider="foundry",
            model_name=self.config.query_api_name,
            conversation_id=context_id,
        )

    def classify(self, message: str) -> Literal[TaskState.input_required, TaskState.completed, TaskState.failed]:
        # Simple classification logic (to be replaced with actual logic)
        if (
            "need more info" in message.lower()
            or "please provide" in message.lower()
            or "before proceeding" in message.lower()
            or "before i proceed" in message.lower()
            or "in order to proceed" in message.lower()
        ):
            return TaskState.input_required
        elif "Failed to create ticket" in message:
            return TaskState.failed
        else:
            return TaskState.completed

    async def ainvoke(self, input_data: dict[str, Any], *, config: Optional[dict[str, Any]] = None) -> StreamEvent:
        """Async invoke with automatic cost tracking flush.

        Overrides parent to flush cost tracking after each invocation,
        since local sub-agents don't have cleanup lifecycle hooks.

        Args:
            input_data: Input data matching SubAgentInput schema
            config: Optional parent config for checkpoint isolation and cost tracking

        Returns:
            The final StreamEvent from the stream
        """
        try:
            # Call parent implementation
            result = await super().ainvoke(input_data, config=config)
            return result
        finally:
            # Always flush cost tracking after invocation
            # This ensures records are sent immediately, not batched
            if self._cost_tracking_enabled:
                try:
                    await self.flush_cost_tracking()
                    logger.debug(f"Flushed cost tracking for Foundry agent '{self.config.name}'")
                except Exception as e:
                    logger.warning(f"Failed to flush cost tracking for '{self.config.name}': {e}")

    async def acleanup(self):
        """Clean up resources (close Foundry client and flush cost tracking)."""
        # Flush cost tracking before cleanup
        await self.flush_cost_tracking()

        if self._client is not None:
            # Note: AsyncFoundryClient may need explicit cleanup
            # Check SDK documentation for proper cleanup
            self._client = None
            logger.info(f"Cleaned up Foundry client for {self.config.name}")


def create_foundry_local_subagent(
    config: LocalFoundrySubAgentConfig,
    user: dict[str, Any],
    backend_url: Optional[str] = None,
    sub_agent_id: Optional[int] = None,
) -> CompiledSubAgent:
    """Create a dynamic local sub-agent from configuration.

    Factory function that creates a CompiledSubAgent wrapping a FoundryLocalAgentRunnable.
    This can be registered in the orchestrator's subagent_registry for use with the task tool.

    Args:
        config: LocalFoundrySubAgentConfig with name, description, and Foundry settings
        user: User context dict with user_sub, email, name
        backend_url: Backend URL for cost tracking (optional)
        sub_agent_id: Sub-agent ID for cost attribution (optional)

    Returns:
        CompiledSubAgent that can be registered with the orchestrator

    Example:
        config = LocalFoundrySubAgentConfig(
            name="data-analyst",
            description="Analyzes data and generates insights",
            system_prompt="You are a data analysis expert...",
            mcp_tools=["query_database", "generate_chart"],  # Whitelist specific tools
        )
        subagent = create_foundry_local_subagent(
            config, user,
            backend_url="https://backend.example.com",
            sub_agent_id=123
        )
        subagent_registry["data-analyst"] = subagent
    """
    runnable = FoundryLocalAgentRunnable(
        config=config,
        user=user,
        backend_url=backend_url,
        sub_agent_id=sub_agent_id,
    )

    return CompiledSubAgent(
        name=config.name,
        description=config.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
