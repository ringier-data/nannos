import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import (
    new_agent_text_message,
    new_task,
)
from a2a.utils.errors import ServerError

from ..models.config import UserConfig

# from google.adk.sessions import InMemorySessionService
from .agent import OrchestratorDeepAgent
from .budget_guard import get_budget_guard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class OrchestratorDeepAgentExecutor(AgentExecutor):
    """OrchestratorDeepAgent Executor Example."""

    def __init__(self):
        self.agent = OrchestratorDeepAgent()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the agent task, handling both new requests and resumption from interrupts.

        This method implements the resumption mechanism for LangGraph interrupts:

        1. For new requests: Execute the agent normally using agent.stream()
        2. For resumption: Detect auth completion patterns in the user message
        3. If resuming: Use Command(resume=query) to resume from interrupt
        4. The graph resumes from where interrupt() was called

        The resumption happens when:
        - The graph has pending interrupts
        - The user message contains auth completion patterns
        - The Command(resume=value) is streamed to the graph

        This allows seamless continuation after authentication without losing context.

        Authentication:
        - User identity is validated by OidcAuthMiddleware before this method is called
        - Only authenticated users with valid OIDC tokens can reach this point
        - User info is available in request.state.user (set by middleware) but not directly
          accessible here since A2A SDK abstracts the request layer
        """
        # Note: Authentication is enforced at the middleware layer
        # All requests reaching this method have already been authenticated
        logger.debug("Executing request from authenticated user")

        error = self._validate_request(context)
        if error:
            raise ServerError(error=InvalidParamsError())

        query = context.get_user_input()
        task = context.current_task
        logger.debug(f"Starting execution for query: {query}")
        logger.debug(f"Current task: {task}")
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # ZERO-TRUST: Extract verified user_id and token from call_context (set by RequestContextBuilder)
        if context.call_context and hasattr(context.call_context, "state"):
            try:
                user_id = context.call_context.state["user_id"]
                user_token = context.call_context.state["user_token"]
                user_name = context.call_context.state["user_name"]
                user_email = context.call_context.state["user_email"]
            except KeyError as e:
                logger.error(f"[ZERO-TRUST] Missing expected user context key: {e}")
                raise ServerError(error=InvalidParamsError()) from e
        else:
            logger.error("[ZERO-TRUST] No user_token found in call_context - authentication may have failed")
            raise ServerError(error=InvalidParamsError())

        logger.info(f"[ZERO-TRUST] Using verified user_id for graph retrieval: {user_id}")

        # Extract model preference from message metadata (client-specified, optional)
        model_choice = None
        if context.message and hasattr(context.message, "metadata") and context.message.metadata:
            model_choice = context.message.metadata.get("model")
            if model_choice:
                logger.info(f"Model preference from client metadata: {model_choice}")

        # Check budget guard before processing request
        budget_guard = get_budget_guard()
        if budget_guard and budget_guard.is_locked:
            status = budget_guard.get_status()
            logger.warning(
                f"Request rejected due to budget lock. "
                f"Usage: {status.current_usage:,}/{status.token_limit:,} tokens. "
                f"Reason: {status.lock_reason}"
            )
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    "Service temporarily unavailable: Monthly token budget has been exceeded. "
                    "Please contact an administrator to increase the budget or wait until next month.",
                    task.context_id,
                    task.id,
                ),
                final=True,
            )
            return

        try:
            # Create config for graph execution with interrupt support
            config = {"configurable": {"thread_id": task.context_id}}
            user_config = UserConfig(
                user_id=user_id,
                access_token=user_token,
                name=user_name,
                email=user_email,
                model=model_choice,
            )

            # Check if we need to resume from an interrupt
            # Get or create graph for this user's configuration
            # ZERO-TRUST: Pass verified user_id and user_token from call_context
            graph = await self.agent.get_or_create_graph(
                model_type=user_config.model if user_config.model else self.agent._default_model_type
            )
            current_state = graph.get_state(config)  # type: ignore

            # Check if the graph is currently interrupted and this might be a resume request
            if hasattr(current_state, "interrupts") and current_state.interrupts:
                resume_value = query
                logger.info("Resuming from interrupt based on user input")
            else:
                resume_value = None
                logger.info("Normal execution (not resuming from interrupt)")

            async for item in self.agent.stream(query, user_config, task.context_id, resume=resume_value):
                current_state = graph.get_state(config)  # type: ignore
                if hasattr(current_state, "interrupts") and current_state.interrupts:
                    is_final = False
                else:
                    is_final = True
                await self._handle_stream_item(item, updater, task, is_final=is_final)
        except Exception as e:
            logger.error(f"An error occurred while streaming the response: {e.__class__.__name__}: {e}")
            raise ServerError(error=InternalError()) from e

    async def _handle_stream_item(self, item, updater, task, is_final: bool) -> None:
        """Handle a stream item from the agent and update the task accordingly."""
        # item is an AgentStreamResponse object
        state = item.state
        content = item.content

        # Handle different A2A task states
        if state == TaskState.working and not is_final:
            # Status update or intermediate progress
            logger.info(f"Emitting status update: {content}")
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=False,  # Not final - keep the task open
            )

        elif state == TaskState.working and is_final:
            logger.info(f"Contradictory working final state, treating as working: {content}")
            # Treat as working
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=False,  # Not final - keep the task open
            )

        elif state == TaskState.failed:
            # Handle failure state
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=True,
            )

        elif state == TaskState.input_required:
            # User input required - leave task in input_required state
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=False,
            )

        elif state == TaskState.auth_required:
            # Authentication required - leave task in auth_required state
            await updater.update_status(
                TaskState.auth_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=False,
            )

        elif state == TaskState.completed and is_final:
            # Task completed successfully
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                name="orchestrator_result",
            )
            await updater.complete()

        elif state == TaskState.completed and not is_final:
            logger.info(f"Contradictory completed non-final state, treating as input_required: {content}")
            # User input required - leave task in input_required state
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                final=False,
            )
        else:
            # Unknown state - log warning and treat as completed
            logger.warning(f"Unknown task state: {state}, treating as completed")
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                name="orchestrator_result",
            )
            await updater.complete()

    def _validate_request(self, context: RequestContext) -> bool:
        return False

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())
