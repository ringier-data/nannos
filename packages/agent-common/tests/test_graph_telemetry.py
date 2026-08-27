"""LangGraph interrupt/resume telemetry.

The regression under test: LangGraph dispatched ``on_interrupt`` / ``on_resume``
to the OTel auto-instrumentation handler, which has neither method, so every
HITL pause logged an AttributeError warning and no span was recorded.
"""

from __future__ import annotations

import pytest
from agent_common.core.graph_telemetry import (
    GraphLifecycleTelemetryHandler,
    install_graph_lifecycle_telemetry,
    reset_graph_lifecycle_telemetry_for_tests,
)
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.callbacks import (
    GraphCallbackHandler,
    GraphInterruptEvent,
    GraphResumeEvent,
    get_async_graph_callback_manager_for_config,
    get_sync_graph_callback_manager_for_config,
)
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _NonGraphHandler(BaseCallbackHandler):
    """Stands in for the injected OpenTelemetryCallbackHandler.

    Same shape as the real thing: a plain LangChain handler with no graph
    lifecycle methods, so dispatching to it raises AttributeError.
    """


class _ForeignGraphHandler(GraphCallbackHandler):
    """A graph handler someone else registered; must survive the filter."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_interrupt(self, event: GraphInterruptEvent) -> None:
        self.seen.append("interrupt")

    def on_resume(self, event: GraphResumeEvent) -> None:
        self.seen.append("resume")


class _Interrupt:
    """Minimal stand-in for langgraph.types.Interrupt."""

    def __init__(self, interrupt_id: str, value: dict) -> None:
        self.id = interrupt_id
        self.value = value


def _interrupt_event() -> GraphInterruptEvent:
    return GraphInterruptEvent(
        run_id=None,
        status="pending",
        checkpoint_id="chk-1",
        checkpoint_ns=("dynamic-alloy-ai-assistant", "tools"),
        interrupts=(
            _Interrupt(
                "0e56e5e4c5eb66b223a9d24f08ab7244",
                {"client_action_request": {"directive": {"kind": "read_current_page"}}},
            ),
        ),
    )


def _resume_event() -> GraphResumeEvent:
    return GraphResumeEvent(
        run_id=None,
        status="input",
        checkpoint_id="chk-1",
        checkpoint_ns=(),
    )


def _inject_auto_instrumentation_handler(manager):
    """Reproduce how the OTel handler actually gets in.

    ``opentelemetry-instrumentation-langchain`` wraps
    ``BaseCallbackManager.__init__`` and appends its handler there — which is
    *after* LangGraph's ``_filter_graph_handlers()`` has run. Passing the handler
    to the factory instead would be filtered out and prove nothing.
    """
    handler = _NonGraphHandler()
    manager.handlers.append(handler)
    manager.inheritable_handlers.append(handler)
    return manager


@pytest.fixture
def spans():
    """Capture spans, and restore the global provider afterwards."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = otel_trace.get_tracer_provider()
    # set_tracer_provider() only takes effect once per process, so go through the
    # module-private hook the SDK uses for exactly this case.
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _fresh_install():
    reset_graph_lifecycle_telemetry_for_tests()
    yield
    reset_graph_lifecycle_telemetry_for_tests()


def test_handler_records_interrupt_span(spans):
    GraphLifecycleTelemetryHandler().on_interrupt(_interrupt_event())

    (span,) = spans.get_finished_spans()
    assert span.name == "langgraph.interrupt"
    assert span.attributes["langgraph.lifecycle"] == "interrupt"
    assert span.attributes["langgraph.status"] == "pending"
    assert span.attributes["langgraph.checkpoint_id"] == "chk-1"
    assert span.attributes["langgraph.checkpoint_ns"] == "dynamic-alloy-ai-assistant:tools"
    assert span.attributes["langgraph.interrupt_count"] == 1
    assert span.attributes["langgraph.interrupt_ids"] == "0e56e5e4c5eb66b223a9d24f08ab7244"
    # Why the graph paused, without what it paused on.
    assert span.attributes["langgraph.interrupt_kinds"] == "client_action_request"


def test_interrupt_payload_values_are_not_recorded(spans):
    GraphLifecycleTelemetryHandler().on_interrupt(_interrupt_event())

    (span,) = spans.get_finished_spans()
    serialized = repr(sorted(span.attributes.items()))
    assert "read_current_page" not in serialized
    assert "directive" not in serialized


def test_handler_records_resume_span(spans):
    GraphLifecycleTelemetryHandler().on_resume(_resume_event())

    (span,) = spans.get_finished_spans()
    assert span.name == "langgraph.resume"
    assert span.attributes["langgraph.lifecycle"] == "resume"
    # Empty namespace = the standalone root graph.
    assert span.attributes["langgraph.checkpoint_ns"] == ""
    assert "langgraph.interrupt_count" not in span.attributes


def test_telemetry_failure_does_not_propagate(monkeypatch, spans):
    """A broken exporter must not surface as a failed turn."""
    import agent_common.core.graph_telemetry as module

    def boom():
        raise RuntimeError("tracer exploded")

    monkeypatch.setattr(module, "_tracer", boom)
    GraphLifecycleTelemetryHandler().on_interrupt(_interrupt_event())
    assert spans.get_finished_spans() == ()


def test_install_is_idempotent():
    assert install_graph_lifecycle_telemetry() is True
    assert install_graph_lifecycle_telemetry() is True


def test_install_evicts_non_graph_handlers_and_subscribes_ours():
    """The core regression.

    A plain LangChain handler reaching the manager is what produced
    ``AttributeError: ... has no attribute 'on_interrupt'``. After one dispatch
    it is gone, ours is present, and a legitimate graph handler is untouched.
    """
    install_graph_lifecycle_telemetry()

    foreign = _ForeignGraphHandler()
    manager = _inject_auto_instrumentation_handler(
        get_sync_graph_callback_manager_for_config({"callbacks": [foreign]})
    )
    # Present on the manager despite LangGraph's filter — this is the bug.
    assert _NonGraphHandler in [type(h) for h in manager.handlers]

    manager.on_interrupt(_interrupt_event())

    kinds = [type(h) for h in manager.handlers]
    assert _NonGraphHandler not in kinds
    assert _ForeignGraphHandler in kinds
    assert GraphLifecycleTelemetryHandler in kinds


def test_patching_survives_a_factory_imported_before_install():
    """Import order must not defeat the patch.

    ``langgraph.pregel.main`` binds the manager factories by name at module
    import — long before any startup hook runs. This module's own top-level
    import of the same factory reproduces that, so a fix that wrapped the
    factory instead of the manager class would regress here.
    """
    install_graph_lifecycle_telemetry()

    manager = _inject_auto_instrumentation_handler(
        get_sync_graph_callback_manager_for_config({})
    )
    manager.on_resume(_resume_event())

    assert [type(h) for h in manager.handlers] == [GraphLifecycleTelemetryHandler]
    # inheritable_handlers is what nested graphs copy; leaving it dirty would let
    # a subgraph re-acquire the handler we just evicted.
    assert [type(h) for h in manager.inheritable_handlers] == []


def test_patched_manager_dispatches_to_both_handlers(spans):
    install_graph_lifecycle_telemetry()

    foreign = _ForeignGraphHandler()
    manager = _inject_auto_instrumentation_handler(
        get_sync_graph_callback_manager_for_config({"callbacks": [foreign]})
    )

    manager.on_interrupt(_interrupt_event())
    manager.on_resume(_resume_event())

    assert foreign.seen == ["interrupt", "resume"]
    assert [s.name for s in spans.get_finished_spans()] == [
        "langgraph.interrupt",
        "langgraph.resume",
    ]


@pytest.mark.asyncio
async def test_async_manager_dispatches_sync_handler(spans):
    """``ahandle_event`` runs a sync handler inline only when run_inline is set."""
    install_graph_lifecycle_telemetry()

    manager = _inject_auto_instrumentation_handler(
        get_async_graph_callback_manager_for_config({})
    )
    await manager.on_interrupt(_interrupt_event())

    assert [s.name for s in spans.get_finished_spans()] == ["langgraph.interrupt"]


def test_span_is_parented_to_the_current_span(spans):
    """The pause must land inside the turn, not as an orphan root."""
    install_graph_lifecycle_telemetry()
    tracer = otel_trace.get_tracer("test")

    with tracer.start_as_current_span("send_message") as parent:
        GraphLifecycleTelemetryHandler().on_interrupt(_interrupt_event())
        expected_parent = parent.get_span_context().span_id

    interrupt_span = next(s for s in spans.get_finished_spans() if s.name == "langgraph.interrupt")
    assert interrupt_span.parent is not None
    assert interrupt_span.parent.span_id == expected_parent
