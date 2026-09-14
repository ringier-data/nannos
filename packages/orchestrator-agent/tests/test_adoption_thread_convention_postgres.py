"""The one thread convention, end to end against a real Postgres checkpointer.

ADR-0008 §5 replaced fork-on-adopt with a naming agreement: agent-runner writes
a scheduled run to ``local_sub_agent_thread_id(run_ctx, name)``, the
orchestrator seeds ``{"context_id": run_ctx}`` on adoption, and the next
delegation derives the same thread and continues the run's conversation with no
checkpoint copying. The whole mechanism is two processes agreeing on a string,
and the only tests of it assert that one function returns what the other
function returns.

This is the replacement for the deleted ``test_adoption_postgres_fork``, which
was the only test running two service-shaped graphs over real checkpoint
tables. It keeps that harness and crosses the same two boundaries the
MemorySaver unit tests cannot:

1. a real ``AsyncPostgresSaver`` over ``build_checkpointer_pool`` (schema
   ``docstore``, mirroring the reference deployment where agent-runner and the
   orchestrator share checkpoint tables) — history must round-trip through real
   (de)serialization and blob storage;
2. a cross-VARIANT graph load: the run is written by an agent-runner-shaped
   graph (no HITL middleware — scheduled runs are fail-open) and continued by an
   orchestrator-shaped graph (HITL middleware attached), a different
   middleware/state stack over the same tables.

Marked ``integration`` because it needs Docker. It makes **no LLM calls** — the
model is scripted — so unlike the rest of that tier it costs nothing to run.
Skips when Docker is unavailable.
"""

import uuid
from typing import Any

import pytest
from a2a.types import TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.threads import local_sub_agent_thread_id, seal_dangling_tool_calls
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from agent_common.core.graph_utils import build_sub_agent_graph
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.core.agent import _build_adoption_seed
from tests.test_scheduled_run_adoption import VALIDATED, _local_runnable, _registry

AGENT_NAME = "report-agent"
PG_SCHEMA = "docstore"  # mirror the reference deployment
# Same image console-backend's test suite provisions; plain postgres works too
# since the checkpointer needs no extensions.
PG_IMAGES = ["docker.rcplus.io/pgvector/pgvector:pg16", "postgres:16"]


@pytest.fixture(scope="session")
def postgres_dsn():
    """Start a throwaway Postgres container for the session (console pattern)."""
    import os

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed")

    # The fixture stops its container itself; the ryuk reaper sidecar only gets
    # in the way when the first image attempt fails (its fixed per-session name
    # then blocks the fallback attempt with a 409).
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

    last_error: Exception | None = None
    for image in PG_IMAGES:
        try:
            container = PostgresContainer(image=image, username="docstore", dbname="nannos")
            container.start()
        except Exception as e:  # docker missing, image pull denied, ...
            last_error = e
            continue
        try:
            yield {
                "host": container.get_container_host_ip(),
                "port": str(container.get_exposed_port(5432)),
                "db": container.dbname,
                "user": container.username,
                "password": container.password,
            }
        finally:
            container.stop()
        return
    pytest.skip(f"No Postgres container available: {last_error}")


class ScriptedChatModel(BaseChatModel):
    """Deterministic chat model: pops one scripted AIMessage per call."""

    script: list[AIMessage]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs) -> "ScriptedChatModel":
        return self


@tool
def lookup(query: str) -> str:
    """Look up a business figure."""
    return "sales were up 4%"


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _open_saver(dsn: dict[str, str]):
    """Build the real pool + saver the way both services do in production."""
    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import build_checkpointer_pool

    # The services' POSTGRES_SCHEMA is pre-provisioned; mirror that.
    async with await psycopg.AsyncConnection.connect(
        host=dsn["host"],
        port=dsn["port"],
        dbname=dsn["db"],
        user=dsn["user"],
        password=dsn["password"],
        autocommit=True,
    ) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")

    pool = build_checkpointer_pool(
        host=dsn["host"],
        port=dsn["port"],
        db=dsn["db"],
        user=dsn["user"],
        password=dsn["password"],
        schema=PG_SCHEMA,
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    return pool, saver


def _agent_runner_shaped_graph(saver, script: list[AIMessage]):
    """The graph shape agent-runner runs scheduled local/automated agents with:
    no HITL middleware (fail-open), no sandbox, plain backend."""
    return build_sub_agent_graph(
        model=ScriptedChatModel(script=script),
        tools=[lookup],
        system_prompt="You are the report agent.",
        checkpointer=saver,
        store=None,
        cost_logger=None,
        response_format=None,
        exclude_deep_agents_middlewares=False,
    )


def _orchestrator_shaped_graph(saver, script: list[AIMessage]):
    """The graph shape the orchestrator's dynamic delegation runs: same builder,
    but with the HITL middleware attached (a different middleware and state
    stack over the same checkpoint tables)."""
    return build_sub_agent_graph(
        model=ScriptedChatModel(script=script),
        tools=[lookup],
        system_prompt="You are the report agent.",
        checkpointer=saver,
        store=None,
        cost_logger=None,
        response_format=None,
        exclude_deep_agents_middlewares=False,
        hitl_guarded_tools={"lookup": True},
    )


def _agent_runner_thread(run_ctx: str) -> str:
    """The thread agent-runner executes a scheduled run on (``agent/core.py``)."""
    return local_sub_agent_thread_id(run_ctx, AGENT_NAME)


def _adopting_delegation_thread(conversation_id: str) -> str:
    """The thread the next delegation lands on, through the production waterfall.

    ``_build_adoption_seed`` writes the ``a2a_tracking`` record;
    ``_extract_tracking_ids`` picks the context id out of it and ``get_thread_id``
    turns that into the checkpoint thread. Nothing here is re-implemented.
    """
    runnable = _local_runnable(AGENT_NAME)
    _, tracking_key, record = _build_adoption_seed(dict(VALIDATED), _registry(runnable, key=AGENT_NAME))
    sub_input = SubAgentInput(
        messages=[],
        a2a_tracking={tracking_key: record},
        orchestrator_conversation_id=conversation_id,
    )
    context_id, _ = DynamicLocalAgentRunnable._extract_tracking_ids(runnable, sub_input)
    return DynamicLocalAgentRunnable.get_thread_id(runnable, context_id, sub_input)


def _unanswered_tool_calls(messages: list) -> list[str]:
    """Tool-call ids with no ``ToolMessage`` — what a provider rejects the turn for."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        call["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for call in (m.tool_calls or [])
        if call.get("id") and call["id"] not in answered
    ]


@pytest.fixture
async def persistent_task_store(postgres_dsn):
    """The orchestrator's own A2A task store, installed for local sub-agents.

    ``main.py`` calls ``set_local_task_store(task_store)`` at startup with the
    Postgres-backed store its HTTP handler uses — the whole basis of ADR-0008's
    "a delegation parked on an approval survives a restart". The global is reset
    on teardown so the per-event-loop in-memory default is back for other tests.
    """
    from a2a.server.tasks import DatabaseTaskStore
    from agent_common.a2a.local_server import server as local_server_module
    from agent_common.a2a.local_server import set_local_task_store
    from sqlalchemy import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=postgres_dsn["user"],
            password=postgres_dsn["password"],
            host=postgres_dsn["host"],
            port=int(postgres_dsn["port"]),
            database=postgres_dsn["db"],
        )
    )
    store = DatabaseTaskStore(engine, create_table=True)
    set_local_task_store(store)
    try:
        yield store
    finally:
        # No public "uninstall"; the module global is the installation.
        local_server_module._shared_task_store = None
        await engine.dispose()


@pytest.mark.integration
class TestThreadConventionAgainstRealPostgres:
    @pytest.mark.asyncio
    async def test_agent_runner_thread_is_continued_by_an_adopting_delegation(self, postgres_dsn):
        """The load-bearing claim of ADR-0008 §5, over real checkpoint tables.

        agent-runner writes the run; the orchestrator's adoption seed derives the
        same thread; an orchestrator-shaped graph resumes it with the run's full
        history and appends a new turn — no checkpoint is copied.
        """
        pool, saver = await _open_saver(postgres_dsn)
        try:
            # VALIDATED["conversation_id"] is the run ctx the seed is built from,
            # so the run must be written under exactly that id.
            run_ctx = VALIDATED["conversation_id"]
            run_thread = _agent_runner_thread(run_ctx)

            # 1. The scheduled run, exactly as agent-runner executes it.
            writer = _agent_runner_shaped_graph(
                saver,
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"id": "call-1", "name": "lookup", "args": {"query": "sales"}, "type": "tool_call"}
                        ],
                    ),
                    AIMessage(content="Report: sales were up 4%."),
                ],
            )
            await writer.ainvoke(
                {"messages": [HumanMessage(content="Summarize yesterday's sales.")]}, _config(run_thread)
            )
            run_state = await writer.aget_state(_config(run_thread))
            run_contents = [m.content for m in run_state.values["messages"]]
            assert "Report: sales were up 4%." in run_contents
            assert "sales were up 4%" in run_contents  # the ToolMessage

            # 2. The convention itself: the adopting conversation derives the very
            #    thread agent-runner wrote. This is the whole mechanism.
            assert _adopting_delegation_thread(f"orch-ctx-{uuid.uuid4()}") == run_thread

            # 3. The follow-up delegation, on the orchestrator-shaped graph — a
            #    state stack the writer never had, over the writer's checkpoint.
            reader = _orchestrator_shaped_graph(
                saver, [AIMessage(content="As established, sales were up 4%; here is the refinement.")]
            )
            seen = await reader.aget_state(_config(run_thread))
            assert [m.content for m in seen.values["messages"]] == run_contents

            result = await reader.ainvoke(
                {"messages": [HumanMessage(content="Refine the DACH numbers.")]}, _config(run_thread)
            )
            contents = [m.content for m in result["messages"]]
            assert contents[: len(run_contents)] == run_contents
            assert "Refine the DACH numbers." in contents
            assert contents[-1] == "As established, sales were up 4%; here is the refinement."
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_a_crashed_run_is_sealed_in_the_next_turns_input(self, postgres_dsn):
        """A run that died mid-tool is resumable, and the checkpoint is never edited.

        The fork used to seal the dangling call while copying. There is no copy
        any more: ``_astream_impl`` reads the thread, computes the seals and
        prepends them to *this turn's input*. What must hold is the invariant
        providers enforce — no ``tool_use`` without a ``tool_result`` — through
        real Postgres (de)serialization.
        """
        pool, saver = await _open_saver(postgres_dsn)
        try:
            run_thread = _agent_runner_thread(f"run-ctx-{uuid.uuid4()}")

            # Emulate the mid-tool death: the last committed checkpoint ends right
            # after the model emitted the tool call; the results never landed.
            writer = _agent_runner_shaped_graph(saver, [AIMessage(content="unused")])
            await writer.aupdate_state(
                _config(run_thread),
                {
                    "messages": [
                        HumanMessage(content="Summarize yesterday's sales."),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"id": "call-lost", "name": "lookup", "args": {"query": "sales"}, "type": "tool_call"}
                            ],
                        ),
                    ]
                },
            )

            reader = _orchestrator_shaped_graph(saver, [AIMessage(content="Retrying the lookup now — done.")])
            checkpoint_msgs = list((await reader.aget_state(_config(run_thread))).values["messages"])
            assert _unanswered_tool_calls(checkpoint_msgs) == ["call-lost"]

            # Exactly what _astream_impl does: seals go in the input, ahead of the
            # human message. The checkpoint is not touched.
            seals = seal_dangling_tool_calls(checkpoint_msgs)
            assert [s.tool_call_id for s in seals] == ["call-lost"]

            follow_up = HumanMessage(content="Please retry.")
            result = await reader.ainvoke({"messages": [*seals, follow_up]}, _config(run_thread))

            messages = result["messages"]
            assert _unanswered_tool_calls(messages) == [], "a tool_use with no tool_result — providers reject this turn"
            sealer = next(m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call-lost")
            assert "ended before this tool call" in sealer.content
            assert sealer.status == "error"
            assert messages[-1].content == "Retrying the lookup now — done."
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_two_conversations_adopting_one_run_interleave_on_its_thread(self, postgres_dsn):
        """What the run-keyed thread costs, demonstrated over real checkpoint tables.

        Both conversations derive the run's own thread, so the second reads the
        first's turn as its sub-agent's memory. Unreachable in production — a run
        is adoptable exactly once (see
        ``test_adoption_puts_every_conversation_on_the_runs_own_thread`` for the
        three constraints) — so this is here to show what breaks if that stops
        being true, with the interleaving visible rather than inferred.
        """
        pool, saver = await _open_saver(postgres_dsn)
        try:
            run_ctx = VALIDATED["conversation_id"]
            run_thread = _agent_runner_thread(run_ctx)

            writer = _agent_runner_shaped_graph(saver, [AIMessage(content="Report: sales were up 4%.")])
            await writer.ainvoke({"messages": [HumanMessage(content="Summarize sales.")]}, _config(run_thread))

            thread_a = _adopting_delegation_thread("orch-ctx-a")
            thread_b = _adopting_delegation_thread("orch-ctx-b")
            assert thread_a == thread_b == run_thread

            # Conversation A delegates: its turn lands on the run's thread.
            graph_a = _orchestrator_shaped_graph(saver, [AIMessage(content="A's refinement.")])
            await graph_a.ainvoke({"messages": [HumanMessage(content="A asks about DACH.")]}, _config(thread_a))

            # Conversation B delegates and sees A's private turn as its own memory.
            graph_b = _orchestrator_shaped_graph(saver, [AIMessage(content="B's refinement.")])
            seen_by_b = [m.content for m in (await graph_b.aget_state(_config(thread_b))).values["messages"]]
            assert "A asks about DACH." in seen_by_b
            assert "A's refinement." in seen_by_b
        finally:
            await pool.close()


@pytest.mark.integration
class TestParkedTaskSurvivesARestart:
    """ADR-0008 §6: local tasks live in the orchestrator's persistent task store.

    The claim is that a delegation parked on an approval survives a restart like
    any other task. A restart is what makes this non-trivial: the ``LocalA2AServer``
    — its request handler, its ``InMemoryQueueManager``, its per-conversation
    locks — is rebuilt per turn and lost on restart. Only the task store outlives
    it, and only if one was installed.
    """

    @pytest.mark.asyncio
    async def test_a_parked_delegation_is_found_again_by_a_fresh_server(self, persistent_task_store):
        from app.middleware.dynamic_tool_dispatch import delegation_task_id
        from tests.support.graph_harness import runtime_context, scripted_graph, task_call, turn_config, user_turn
        from tests.support.mock_subagents import MockSubAgent
        from tests.support.scripted_model import ScriptedChatModel

        # A production conversation id: the task store's context_id column is
        # VARCHAR(36), exactly a UUID and not one character more.
        thread = str(uuid.uuid4())
        github = MockSubAgent("github-agent", "GitHub.", reply="you are aartaria", approval="github_get_me")
        model = ScriptedChatModel(responses=[task_call("github-agent", "who am I", call_id="call-task")])

        state = await scripted_graph(model).ainvoke(
            user_turn("who am I on github"), config=turn_config(thread), context=runtime_context(github)
        )
        assert state.get("__interrupt__"), "precondition: the turn parks on the approval"

        task_id = delegation_task_id(thread, "call-task")
        assert (await github.aget_task(task_id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED

        # The restart: every in-process server object is gone. A brand-new
        # runnable builds a brand-new LocalA2AServer over the same store.
        restarted = MockSubAgent("github-agent", "GitHub.", reply="you are aartaria", approval="github_get_me")
        assert restarted.local_server.task_store is persistent_task_store

        recovered = await restarted.aget_task(task_id)
        assert recovered is not None, "the parked delegation did not survive the restart"
        assert recovered.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert recovered.context_id == thread
