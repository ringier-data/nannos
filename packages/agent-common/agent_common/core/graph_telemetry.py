"""OpenTelemetry spans for LangGraph interrupt / resume.

WHY THIS EXISTS
---------------
LangGraph dispatches two lifecycle events that no other callback carries:
``on_interrupt`` (the graph paused on an ``interrupt()``) and ``on_resume``
(it picked a checkpoint back up). Every HITL turn and every client-action
round trip crosses both. Neither reached a trace, and each one logged a
warning instead::

    Error in OpenTelemetryCallbackHandler.on_interrupt callback:
      AttributeError("'OpenTelemetryCallbackHandler' object has no attribute 'on_interrupt'")

Two separate faults produced that line:

1. ``langgraph.callbacks`` documents that "only handlers that inherit from
   ``GraphCallbackHandler`` receive these lifecycle events", and its
   ``_filter_graph_handlers()`` enforces it — but the filter runs while the
   manager is being *constructed*. The injected AWS OTel auto-instrumentation
   for LangChain appends its ``OpenTelemetryCallbackHandler`` inside
   ``BaseCallbackManager.__init__``, i.e. after the filter, so a handler that
   cannot serve these events ends up subscribed to them anyway.
2. Nothing here implemented the events, so even a correctly filtered manager
   had nobody to dispatch to.

``install_graph_lifecycle_telemetry()`` fixes both: it re-applies the filter at
dispatch time (silencing the warning, and keeping unrelated handlers out of a
callback surface they never opted into) and subscribes one handler of ours that
turns each event into a span.

WHY A SPAN AND NOT A SPAN EVENT
-------------------------------
X-Ray has no first-class representation for OTel span events, so ``add_event()``
would not survive the export. A short child span does, and these traces already
carry plenty of sub-millisecond spans.

PAYLOADS ARE NOT RECORDED
-------------------------
Interrupt values carry whatever the graph paused on — client-action directives,
tool arguments, page contents. Only ids, counts and the top-level payload keys
are attached, never the values.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Sequence
from typing import Any

from opentelemetry import trace as otel_trace

logger = logging.getLogger(__name__)

_TRACER_NAME = "agent-common.langgraph"

# Set once install_graph_lifecycle_telemetry() has patched LangGraph, so a
# second call (several workers importing the same module, tests) is a no-op.
_installed = False

try:
    from langgraph.callbacks import (
        GraphCallbackHandler,
        GraphInterruptEvent,
        GraphResumeEvent,
    )

    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - keeps the module importable without langgraph
    GraphCallbackHandler = object  # type: ignore[assignment,misc]
    GraphInterruptEvent = Any  # type: ignore[assignment,misc]
    GraphResumeEvent = Any  # type: ignore[assignment,misc]
    _LANGGRAPH_AVAILABLE = False


def _tracer() -> otel_trace.Tracer:
    # Fetched per call, not at import: the auto-instrumentation installs the real
    # TracerProvider during startup, and a tracer grabbed before that stays bound
    # to the no-op default for the life of the process.
    return otel_trace.get_tracer(_TRACER_NAME)


def _interrupt_kinds(interrupts: Sequence[Any]) -> list[str]:
    """Top-level keys of each interrupt payload, e.g. ``client_action_request``.

    This is the one part of the payload worth recording: it says *why* the graph
    paused without carrying what it paused on.
    """
    kinds: list[str] = []
    for interrupt in interrupts:
        value = getattr(interrupt, "value", None)
        if isinstance(value, dict):
            kinds.extend(str(key) for key in value)
    return kinds


def _attributes(event: Any, lifecycle: str) -> dict[str, Any]:
    checkpoint_ns = getattr(event, "checkpoint_ns", ()) or ()
    attributes: dict[str, Any] = {
        "langgraph.lifecycle": lifecycle,
        "langgraph.status": str(getattr(event, "status", "") or ""),
        "langgraph.checkpoint_id": str(getattr(event, "checkpoint_id", "") or ""),
        # Joined into one string: the namespace path reads better that way, and
        # X-Ray flattens sequence-valued attributes poorly.
        "langgraph.checkpoint_ns": ":".join(str(part) for part in checkpoint_ns),
    }
    run_id = getattr(event, "run_id", None)
    if run_id is not None:
        attributes["langgraph.run_id"] = str(run_id)

    if lifecycle == "interrupt":
        interrupts = getattr(event, "interrupts", ()) or ()
        attributes["langgraph.interrupt_count"] = len(interrupts)
        ids = [str(getattr(i, "id", "") or "") for i in interrupts]
        joined_ids = ",".join(i for i in ids if i)
        if joined_ids:
            attributes["langgraph.interrupt_ids"] = joined_ids
        kinds = _interrupt_kinds(interrupts)
        if kinds:
            attributes["langgraph.interrupt_kinds"] = ",".join(kinds)
    return attributes


class GraphLifecycleTelemetryHandler(GraphCallbackHandler):  # type: ignore[misc,valid-type]
    """Turns LangGraph interrupt / resume into spans.

    The methods stay sync on purpose. LangGraph has one dispatcher per flavour
    (``_GraphCallbackManager`` calls the handler, ``_AsyncGraphCallbackManager``
    awaits it) and our graphs cross both; LangChain's ``ahandle_event`` runs a
    sync method directly when ``run_inline`` is set, so one implementation
    serves both paths.
    """

    # run_inline keeps the callback on the calling task. Without it the async
    # dispatcher hands the method to a thread executor, where the OTel context
    # var is a copy and the span would be parented to nothing.
    run_inline = True
    # Telemetry never fails a turn.
    raise_error = False

    def _record(self, event: Any, lifecycle: str) -> None:
        try:
            # A point-in-time marker: started and ended together so the span
            # lands at the instant the graph paused or resumed.
            _tracer().start_span(
                f"langgraph.{lifecycle}",
                attributes=_attributes(event, lifecycle),
            ).end()
        except Exception:  # noqa: BLE001 - defensive: never break a graph run
            # Letting this escape would have LangGraph log it, which is the
            # noise this module exists to remove.
            logger.debug("graph lifecycle telemetry failed", exc_info=True)

    def on_interrupt(self, event: GraphInterruptEvent) -> None:
        self._record(event, "interrupt")

    def on_resume(self, event: GraphResumeEvent) -> None:
        self._record(event, "resume")


def _prepare(manager: Any) -> Any:
    """Drop non-graph handlers, then make sure ours is subscribed.

    ``handlers`` is the list LangGraph dispatches over; ``inheritable_handlers``
    is what child runs copy. Both are filtered, so a nested graph cannot
    re-acquire a handler we just removed.
    """
    for attribute in ("handlers", "inheritable_handlers"):
        current = getattr(manager, attribute, None)
        if isinstance(current, list):
            current[:] = [h for h in current if isinstance(h, GraphCallbackHandler)]

    handlers = getattr(manager, "handlers", None)
    if isinstance(handlers, list) and not any(
        isinstance(h, GraphLifecycleTelemetryHandler) for h in handlers
    ):
        handlers.append(GraphLifecycleTelemetryHandler())
    return manager


def _wrap_dispatch(manager_cls: Any, method_name: str) -> bool:
    """Route one dispatch method through ``_prepare`` first.

    The manager *classes* are patched, not the factory functions that build
    them: ``langgraph.pregel.main`` imports those factories by name at module
    import, so whoever patches later loses. A method is looked up on the class
    at call time, which no import order can defeat.
    """
    original = getattr(manager_cls, method_name, None)
    if original is None:  # pragma: no cover - upstream layout change
        logger.warning("%s.%s missing; skipping patch", manager_cls.__name__, method_name)
        return False
    if getattr(original, "__nannos_graph_telemetry__", False):
        return True

    if inspect.iscoroutinefunction(original):

        async def wrapper(self, event):
            _prepare(self)
            return await original(self, event)

    else:

        def wrapper(self, event):
            _prepare(self)
            return original(self, event)

    functools.update_wrapper(wrapper, original)
    wrapper.__nannos_graph_telemetry__ = True  # type: ignore[attr-defined]
    setattr(manager_cls, method_name, wrapper)
    return True


def install_graph_lifecycle_telemetry() -> bool:
    """Patch LangGraph so interrupt / resume reach a handler that traces them.

    Call once per process at startup. Idempotent, order-independent, and safe to
    call before a TracerProvider exists: the patch sits on the manager classes
    so it applies to every manager whenever it was built, and the spans resolve
    the provider lazily. Returns True when the patch is in place.
    """
    global _installed
    if _installed:
        return True
    if not _LANGGRAPH_AVAILABLE:
        logger.debug("langgraph not importable; graph lifecycle telemetry not installed")
        return False

    from langgraph import callbacks as lg_callbacks

    patched_any = False
    for class_name in ("_GraphCallbackManager", "_AsyncGraphCallbackManager"):
        manager_cls = getattr(lg_callbacks, class_name, None)
        if manager_cls is None:  # pragma: no cover - upstream layout change
            logger.warning("langgraph.callbacks.%s missing; skipping patch", class_name)
            continue
        for method_name in ("on_interrupt", "on_resume"):
            patched_any = _wrap_dispatch(manager_cls, method_name) or patched_any

    _installed = patched_any
    if patched_any:
        logger.info("LangGraph interrupt/resume telemetry installed")
    return patched_any


def reset_graph_lifecycle_telemetry_for_tests() -> None:
    """Clear the install latch. Tests only."""
    global _installed
    _installed = False


__all__ = [
    "GraphLifecycleTelemetryHandler",
    "install_graph_lifecycle_telemetry",
    "reset_graph_lifecycle_telemetry_for_tests",
]
