import logging
import os
from collections.abc import AsyncIterable
from typing import List, Literal, Optional

from a2a.types import Task, TaskState
from foundry_sdk import AsyncFoundryClient as PlatformAsyncFoundryClient
from foundry_sdk import Auth
from foundry_sdk import ConfidentialClientAuth as PlatformSDKConfidentialClientAuth
from foundry_sdk import Config as FoundryConfig
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig

logger = logging.getLogger(__name__)


class ConfidentialClientAuth(PlatformSDKConfidentialClientAuth):
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        hostname: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        should_refresh: bool = False,
        *,
        config: Optional[FoundryConfig] = None,
    ) -> None:
        self._default_scopes: List[str] = [
            "api:ontologies-read",
            "api:ontologies-write",
            "api:use-ontologies-read",
            "api:use-ontologies-write",
            "api:functions-read",
            "api:use-functions-read",
        ]

        # If scopes are provided and not empty, append to default scopes
        # Otherwise, use None
        final_scopes = None
        if scopes:
            final_scopes = list(set(self._default_scopes + scopes))

        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            hostname=hostname,
            scopes=final_scopes,
            should_refresh=should_refresh,
            config=config,
        )


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
        self._ontology_rid = ontology_rid or "ri.ontology.main.ontology.10f26c02-9643-4663-91b4-aade7b78ff0f"
        self._client = PlatformAsyncFoundryClient(
            auth=auth,
            hostname=hostname,
            preview=True,  # Required for AsyncFoundryClient (beta)
        )

    async def execute_query(self, query_api_name: str, parameters: dict, version: str = "1.0.0"):
        """
        Execute a Foundry ontology query.

        Args:
            query_api_name: Name of the query in camelCase (e.g., "jiraTicketFunction")
            parameters: Parameters to pass to the query
            version: Query version (default: "1.0.0")

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
            client_id="7d52d6a5d1f0fafe3bc4a4f7ded3c01d",
            client_secret=os.environ["FOUNDRY_CLIENT_SECRET"],
            hostname="https://blumen.palantirfoundry.de",
            should_refresh=True,
            scopes=[
                "api:use-ontologies-read",
                "api:use-ontologies-write",
                "api:use-mediasets-read",
                "api:use-mediasets-write",
                "api:use-aip-agents-read",
                "api:use-aip-agents-write",
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
        # Update session RID for future calls
        if "sessionRid" in results:
            self.foundry_sessions[task.context_id] = results["sessionRid"]
        classification = await self.classify(results.get("message", ""))
        if classification == TaskState.input_required:
            yield AgentStreamResponse(
                state=TaskState.input_required,
                content=results.get("message", "Additional input is required."),
                metadata={"details": results},
            )
        elif classification == TaskState.failed:
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=results.get("message", "Failed to create Jira ticket."),
                metadata={"details": results},
            )
        else:
            yield AgentStreamResponse(
                state=TaskState.completed,
                content=results.get("message", "Jira ticket created successfully."),
                metadata={"details": results},
            )

    async def invoke(self, message: str, session_id: str | None) -> dict:
        if not self.client:
            self.client = await self.get_client()

        # Execute query using camelCase name as per Foundry convention
        result = await self.client.execute_query(
            query_api_name="agentWrapper",  # camelCase, not snake_case!
            parameters={"userInput": message, "sessionRid": session_id},
            version="2.0.0",
        )
        return result

    async def classify(self, message: str) -> Literal[TaskState.input_required, TaskState.completed, TaskState.failed]:
        # Simple classification logic (to be replaced with actual logic)
        if "need more info" in message.lower() or "please provide" in message.lower():
            return TaskState.input_required
        elif "Failed to create ticket" in message:
            return TaskState.failed
        else:
            return TaskState.completed
