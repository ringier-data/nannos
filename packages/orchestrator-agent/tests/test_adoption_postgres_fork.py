"""End-to-end fork-on-adopt against a real Postgres checkpointer.

The production fork crosses two boundaries the MemorySaver unit tests cannot
exercise:

1. a real ``AsyncPostgresSaver`` over ``build_checkpointer_pool`` (schema
   ``docstore``, mirroring the reference deployment where agent-runner and
   the orchestrator share the same checkpoint tables) — the aget_tuple/aput
   copy must round-trip through real (de)serialization and blob storage;
2. a cross-VARIANT graph load: the source checkpoint is written by an
   agent-runner-shaped graph (``build_sub_agent_graph`` with no HITL
   middleware — scheduled runs are fail-open), while the forked thread is
   resumed by an orchestrator-shaped graph (HITL middleware attached, as
   ``create_dynamic_local_subagent`` does), i.e. a different middleware/state
   stack over the same checkpoint.

Follows console-backend's pattern of running tests against a real Postgres
container (tests/conftest.py there); skips when Docker is unavailable.
S3-offloading serde is deployment-specific and out of scope here.
"""

import uuid
from typing import Any

import pytest
from agent_common.core.graph_utils import build_sub_agent_graph
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.middleware.dynamic_tool_dispatch import _maybe_adopt_run_thread

PG_SCHEMA = "docstore"  # mirror the reference deployment
# Same image console-backend's test suite provisions; plain postgres works
# too since the checkpointer needs no extensions.
PG_IMAGES = ["docker.rcplus.io/pgvector/pgvector:pg16", "postgres:16"]


@pytest.fixture(scope="session")
def postgres_dsn():
    """Start a throwaway Postgres container for the session (console pattern)."""
    import os

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed")

    # The fixture stops its container itself; the ryuk reaper sidecar only
    # gets in the way when the first image attempt fails (its fixed
    # per-session name then blocks the fallback attempt with a 409).
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
    """The graph shape the orchestrator's dynamic delegation runs: same
    builder, but with the HITL middleware attached (different middleware and
    state stack over the same checkpoint tables)."""
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


@pytest.mark.integration
class TestForkOnAdoptAgainstRealPostgres:
    @pytest.mark.asyncio
    async def test_agent_runner_checkpoint_resumed_by_orchestrator_graph(self, postgres_dsn):
        pool, saver = await _open_saver(postgres_dsn)
        try:
            run_ctx = f"run-ctx-{uuid.uuid4()}"
            target = _config(f"orch-ctx-{uuid.uuid4()}::dynamic-report-agent")

            # 1. The scheduled run, exactly as agent-runner executes it: bare
            #    thread_id = run contextId, tool round-trip, final answer.
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
                {"messages": [HumanMessage(content="Summarize yesterday's sales.")]},
                _config(run_ctx),
            )
            source_state = await writer.aget_state(_config(run_ctx))
            source_contents = [m.content for m in source_state.values["messages"]]
            assert "Report: sales were up 4%." in source_contents
            assert "sales were up 4%" in source_contents  # the ToolMessage

            # 2. The fork, exactly as the dispatch middleware performs it.
            await _maybe_adopt_run_thread(saver, run_ctx, target, "report-agent")

            # 3. The follow-up delegation, on the orchestrator-shaped graph
            #    (HITL middleware present — a state stack the writer never had).
            reader = _orchestrator_shaped_graph(
                saver,
                [AIMessage(content="As established, sales were up 4%; here is the refinement.")],
            )
            forked_state = await reader.aget_state(target)
            assert [m.content for m in forked_state.values["messages"]] == source_contents

            result = await reader.ainvoke(
                {"messages": [HumanMessage(content="Refine the DACH numbers.")]}, target
            )
            contents = [m.content for m in result["messages"]]
            # The run's full history survived the fork and the new turn ran on top.
            assert contents[: len(source_contents)] == source_contents
            assert "Refine the DACH numbers." in contents
            assert contents[-1] == "As established, sales were up 4%; here is the refinement."

            # 4. The run's own thread is byte-identical after the follow-up.
            source_after = await writer.aget_state(_config(run_ctx))
            assert [m.content for m in source_after.values["messages"]] == source_contents

            # 5. Second delegation in the same conversation must NOT re-fork
            #    (the target now has newer state than the run).
            await _maybe_adopt_run_thread(saver, run_ctx, target, "report-agent")
            re_read = await reader.aget_state(target)
            assert [m.content for m in re_read.values["messages"]] == contents
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_failed_run_with_dangling_tool_call_forks_sealed(self, postgres_dsn):
        """A run that died mid-tool leaves a trailing AIMessage with unanswered
        tool_calls; the fork must seal it in the copy (through real
        serialization) and the orchestrator-shaped graph must resume it."""
        from langchain_core.messages import ToolMessage

        pool, saver = await _open_saver(postgres_dsn)
        try:
            run_ctx = f"run-ctx-{uuid.uuid4()}"
            target = _config(f"orch-ctx-{uuid.uuid4()}::dynamic-report-agent")

            writer = _agent_runner_shaped_graph(
                saver, [AIMessage(content="unused")]
            )
            # Emulate the mid-tool death: the last committed checkpoint ends
            # right after the model emitted the tool call, results never landed.
            await writer.aupdate_state(
                _config(run_ctx),
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

            await _maybe_adopt_run_thread(saver, run_ctx, target, "report-agent")

            reader = _orchestrator_shaped_graph(
                saver, [AIMessage(content="Retrying the lookup now — done.")]
            )
            forked_state = await reader.aget_state(target)
            forked_msgs = forked_state.values["messages"]
            sealer = forked_msgs[-1]
            assert isinstance(sealer, ToolMessage)
            assert sealer.tool_call_id == "call-lost"
            assert "ended before this tool call" in sealer.content

            # The sealed history is resumable by the orchestrator-shaped graph.
            result = await reader.ainvoke(
                {"messages": [HumanMessage(content="Please retry.")]}, target
            )
            assert result["messages"][-1].content == "Retrying the lookup now — done."

            # The run's own thread still ends with the dangling call, unsealed.
            source_after = await writer.aget_state(_config(run_ctx))
            last_source = source_after.values["messages"][-1]
            assert isinstance(last_source, AIMessage)
            assert last_source.tool_calls[0]["id"] == "call-lost"
        finally:
            await pool.close()
