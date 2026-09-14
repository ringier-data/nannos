"""The cost of ``aget_pending_interrupts``, which now runs on every message.

ADR-0008 says the orchestrator's checkpoint probe is gone. It is not gone — it
moved into the sub-agent's in-process executor, which calls
``aget_pending_interrupts`` before *every* message to tell an answer from new
work, fresh delegations included. For a non-sandbox agent that is one
``aget_state`` on a compiled graph the agent already holds.

For a **sandbox-enabled** agent ``_ensure_agent`` deliberately builds no graph
(the sandboxed one is built per invocation), so ``_graph_for_state`` compiles a
stand-in. That stand-in is cached, because otherwise every delegation to a
sandbox agent would pay a graph compilation before the work started — the
regression these tests were written to catch.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable


class _FakeGraph:
    """A compiled graph stand-in whose state is never parked."""

    async def aget_state(self, config):
        return SimpleNamespace(interrupts=())


def _probe_runnable(*, prebuilt_graph: bool) -> tuple[DynamicLocalAgentRunnable, list[int]]:
    """A runnable wired for ``aget_pending_interrupts`` only, counting graph builds.

    ``prebuilt_graph=False`` is the sandbox shape: ``_ensure_agent`` left
    ``_agent`` as ``None`` because the real graph is built per invocation.
    """
    runnable = DynamicLocalAgentRunnable.__new__(DynamicLocalAgentRunnable)
    runnable.config = SimpleNamespace(name="report-agent")
    runnable._agent = _FakeGraph() if prebuilt_graph else None
    runnable._cached_effective_backend_factory = None
    runnable._state_graph = None

    builds: list[int] = []

    def _build_graph(_backend_factory=None, **_kwargs):
        builds.append(1)
        return _FakeGraph()

    runnable._build_graph = _build_graph  # type: ignore[method-assign]

    async def _ensure_agent() -> None:
        return None

    runnable._ensure_agent = _ensure_agent  # type: ignore[method-assign]
    return runnable, builds


async def test_a_non_sandbox_agent_probes_without_building_anything():
    runnable, builds = _probe_runnable(prebuilt_graph=True)

    assert await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}}) == []
    assert await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}}) == []

    assert builds == []


async def test_a_sandbox_agent_reuses_its_state_reading_graph():
    """One stand-in graph, however many probes.

    The executor probes before every message, fresh delegations included, so a
    build here lands on the critical path of every delegation.
    """
    runnable, builds = _probe_runnable(prebuilt_graph=False)

    await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}})
    await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}})

    assert len(builds) == 1


async def test_re_resolution_drops_the_cached_state_graph():
    """``_ensure_agent`` can resolve again after a degraded MCP discovery and rebuild
    the backend factory. The stand-in is built from that factory, so it is dropped
    there rather than outliving the thing it was built from."""
    runnable, builds = _probe_runnable(prebuilt_graph=False)

    await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}})
    runnable._state_graph = None  # what _ensure_agent does when it re-resolves
    await runnable.aget_pending_interrupts({"configurable": {"thread_id": "t"}})

    assert len(builds) == 2


async def test_the_probe_forces_the_standalone_checkpoint_namespace():
    """A sub-agent graph is a standalone root: reading its state under the caller's
    ``checkpoint_ns`` would look at a subgraph's checkpoint and find no interrupts,
    which reads as 'not parked' and would deliver an answer as new work."""
    runnable, _ = _probe_runnable(prebuilt_graph=True)
    seen: dict = {}

    class _RecordingGraph:
        async def aget_state(self, config):
            seen.update(config)
            return SimpleNamespace(interrupts=())

    runnable._agent = _RecordingGraph()

    await runnable.aget_pending_interrupts(
        {"configurable": {"thread_id": "ctx::dynamic-report-agent", "checkpoint_ns": "parent:ns"}}
    )

    assert seen["configurable"]["checkpoint_ns"] == ""
    assert seen["configurable"]["thread_id"] == "ctx::dynamic-report-agent"
