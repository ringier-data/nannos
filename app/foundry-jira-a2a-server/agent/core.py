import logging
import os
from collections.abc import AsyncIterable
from typing import Literal, Optional

from a2a.types import Task, TaskState
from foundry_sdk import AsyncFoundryClient as PlatformAsyncFoundryClient
from foundry_sdk import Auth, ConfidentialClientAuth
from pydantic import BaseModel, Field, model_validator, ValidationError
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig

logger = logging.getLogger(__name__)


class StructuredResponse(BaseModel):
    state: TaskState = Field(
        ...,
        description="The state of the task. In case approval or input is required, the state should be 'input_required'.",
    )
    message: str = Field(..., description="A human-readable message providing details about the task state.")

    @model_validator(mode="before")
    def validate_state(cls, values):
        # input_required -> input-required
        state = values.get("state")
        if isinstance(state, str):
            values["state"] = state.replace("_", "-")
        return values


class AsyncFoundryClient:
    """Simplified async Foundry client - direct access to ontology queries."""

    def __init__(
        self,
        auth: Optional[Auth] = None,
        hostname: Optional[str] = None,
        ontology_rid: Optional[str] = None,
    ):
        """
        Initialize async Foundry client.

        Args:
            auth: Authentication credentials
            hostname: Foundry hostname
            ontology_rid: Ontology RID (uses default if not provided)
        """
        self._ontology_rid = ontology_rid or "ri.ontology.main.ontology.32a49fb7-329f-4ae0-9df8-babc1b29e750"
        self._client = PlatformAsyncFoundryClient(
            auth=auth,
            hostname=hostname,
            preview=True,  # Required for AsyncFoundryClient (beta)
        )

    async def execute_query(self, query_api_name: str, parameters: dict, version: str | None = None):
        """
        Execute a Foundry ontology query.

        Args:
            query_api_name: Name of the query in camelCase (e.g., "jiraTicketFunction")
            parameters: Parameters to pass to the query
            version: Query version (default: None)

        Returns:
            Query execution result
        """
        # Use ontologies.Query.execute (not functions.Query.execute)
        response = await self._client.ontologies.Query.execute(
            ontology=self._ontology_rid,
            query_api_name=query_api_name,
            parameters=parameters,
            version=version,
        )
        logger.info(response)
        return response.value


class FoundryJiraTicketAgent(BaseAgent):
    """Foundry Jira Ticket Agent with async support."""

    def __init__(self) -> None:
        super().__init__()
        self.client: Optional[AsyncFoundryClient] = None
        self.foundry_sessions: dict[str, str] = {}

    async def close(self):
        """Cleanup resources held by the agent."""
        if self.client:
            # If the client had any persistent connections, close them here
            pass

    async def get_client(self):
        """Create async Foundry client."""
        # foundry_sdk.UserTokenAuth(os.environ["FOUNDRY_TOKEN"])
        auth = ConfidentialClientAuth(
            client_id="bda4dfae9127a3dd60ad5af6ce3cf4a0",
            client_secret=os.environ["FOUNDRY_CLIENT_SECRET"],
            hostname="https://blumen.palantirfoundry.de",
            should_refresh=True,
            scopes=[
                "api:use-ontologies-read",
                "api:use-ontologies-write",
                "api:use-aip-agents-read",
                "api:use-aip-agents-write",
                "api:use-mediasets-read",
                "api:use-mediasets-write",
            ],
        )
        return AsyncFoundryClient(
            auth=auth,
            hostname="https://blumen.palantirfoundry.de",
        )

    async def stream(self, message: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Stream responses for a user query following the A2A protocol.

        This is the main entry point for the agent. It processes the user's query
        through the Foundry Jira Ticket function and yields AgentStreamResponse
        objects representing the processing status and results.

        Args:
            message: The user's natural language query
            user_config: User configuration including user_id and access_token
            task: The task context for the current interaction
        Yields:

            AgentStreamResponse objects with state updates and content
        """
        foundry_session_rid = self.foundry_sessions.get(task.context_id)
        results = await self.invoke(message, session_id=foundry_session_rid)
        logger.info(f"Foundry Jira Ticket results: {results}")
        try:
            structured_response = StructuredResponse.model_validate_json(results.get("markdownResponse", "{}"))
        except ValidationError as e:
            logger.error(f"Failed to parse structured response: {e}")
            structured_response = StructuredResponse(
                state=self.classify(results.get("markdownResponse", "")),
                message=results.get("markdownResponse", ""),
            )
        # Update session RID for future calls
        if "sessionRid" in results:
            self.foundry_sessions[task.context_id] = results["sessionRid"]

        if structured_response.state == TaskState.input_required:
            yield AgentStreamResponse(
                state=TaskState.input_required,
                content=structured_response.message or "Additional input is required.",
                metadata={"details": results},
            )
        elif structured_response.state == TaskState.failed:
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=structured_response.message or "Failed to create Jira ticket.",
                metadata={"details": results},
            )
        else:
            yield AgentStreamResponse(
                state=TaskState.completed,
                content=structured_response.message or "Jira ticket created successfully.",
                metadata={"details": results},
            )

    async def invoke(self, message: str, session_id: str | None) -> dict:
        if not self.client:
            self.client = await self.get_client()

        # Execute query using camelCase name as per Foundry convention
        result = await self.client.execute_query(
            query_api_name="agentWrapper",  # camelCase, not snake_case!
            parameters={"userInput": message, "sessionRid": session_id},
            version=None,
        )
        return result

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
