"""Span export filter.

Guards the two properties that make this safe to run in production: the HTTP
*request* span survives (only its per-ASGI-message children go), and anything
that failed survives.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from ringier_a2a_sdk.telemetry.span_filter import (
    FilteringSpanProcessor,
    install_span_export_filter,
    should_drop,
)


class _Status:
    def __init__(self, status_code) -> None:
        self.status_code = status_code


class _Span:
    """Stand-in for a ReadableSpan at on_end."""

    def __init__(self, name, attributes=None, status_code=StatusCode.UNSET, events=()) -> None:
        self.name = name
        self.attributes = attributes or {}
        self.status = _Status(status_code)
        self.events = events


# --- what goes -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "POST /chat/completions http send",
        "POST /chat/completions http receive",
        "GET /api/v1/auth/me http send",
    ],
)
def test_asgi_per_message_events_are_dropped(name):
    assert should_drop(_Span(name)) is True


@pytest.mark.parametrize("statement", ["BEGIN;", "COMMIT;", "ROLLBACK;", ";", "  begin ;  ", ""])
def test_transaction_control_statements_are_dropped(statement):
    assert should_drop(_Span("postgresql", {"db.statement": statement})) is True


def test_current_semantic_convention_attribute_is_honoured():
    """db.query.text superseded db.statement; both spellings appear in the wild."""
    assert should_drop(_Span("postgresql", {"db.query.text": "COMMIT;"})) is True


# --- what stays ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # The request span itself. This is the whole point: dropping the ASGI
        # event children must not touch HTTP request tracing.
        "POST /chat/completions",
        "GET /api/v1/auth/me",
        "send_message",
        # Kept deliberately — asked for.
        "mcp.session",
        # A span that merely mentions the words.
        "http send queue worker",
    ],
)
def test_request_and_wanted_spans_are_kept(name):
    assert should_drop(_Span(name)) is False


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM messages WHERE conversation_id = $1",
        "INSERT INTO messages (conversation_id) VALUES ($1)",
        "UPDATE tasks SET artifacts=$1",
        # Prefix matches must not be swept up with the exact keywords.
        "ROLLBACK TO SAVEPOINT sa_1",
        "COMMIT PREPARED 'tx1'",
        "BEGIN ISOLATION LEVEL SERIALIZABLE",
    ],
)
def test_real_sql_is_kept(statement):
    assert should_drop(_Span("postgresql", {"db.statement": statement})) is False


def test_failed_transaction_control_is_kept():
    """A COMMIT that raised is the one instance of these worth having."""
    span = _Span("postgresql", {"db.statement": "COMMIT;"}, status_code=StatusCode.ERROR)
    assert should_drop(span) is False


def test_chatter_carrying_a_recorded_exception_is_kept():
    span = _Span("POST /chat/completions http send", events=[object()])
    assert should_drop(span) is False


# --- the processor wrapper -------------------------------------------------


class _RecordingProcessor:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[str] = []
        self.shutdowns = 0
        self.flushes = 0

    def on_start(self, span, parent_context=None):
        self.started.append(span.name)

    def on_end(self, span):
        self.ended.append(span.name)

    def shutdown(self):
        self.shutdowns += 1

    def force_flush(self, timeout_millis=30000):
        self.flushes += 1
        return True


def test_on_start_always_delegates_even_for_dropped_spans():
    """A processor may hold per-span state between start and end.

    Swallowing the start half would corrupt it, so filtering happens only at
    on_end.
    """
    inner = _RecordingProcessor()
    processor = FilteringSpanProcessor(inner)
    chatter = _Span("POST /x http send")

    processor.on_start(chatter)
    processor.on_end(chatter)

    assert inner.started == ["POST /x http send"]
    assert inner.ended == []
    assert processor.dropped == 1


def test_lifecycle_calls_pass_through():
    inner = _RecordingProcessor()
    processor = FilteringSpanProcessor(inner)

    assert processor.force_flush(1000) is True
    processor.shutdown()

    assert (inner.flushes, inner.shutdowns) == (1, 1)


def test_a_filter_bug_keeps_the_span(monkeypatch):
    """Never lose telemetry to a fault in the filter itself."""
    import ringier_a2a_sdk.telemetry.span_filter as module

    monkeypatch.setattr(module, "should_drop", lambda span: 1 / 0)
    inner = _RecordingProcessor()
    processor = module.FilteringSpanProcessor(inner)

    processor.on_end(_Span("POST /x http send"))

    assert inner.ended == ["POST /x http send"]


# --- install ---------------------------------------------------------------


@pytest.fixture
def sdk_provider():
    """A real SDK provider as the global one, restored afterwards."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = otel_trace.get_tracer_provider()
    # set_tracer_provider() only takes effect once per process.
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        yield provider, exporter
    finally:
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]


def test_install_filters_spans_from_a_tracer_created_before_install(sdk_provider):
    """The ordering property the design turns on.

    Every instrumentation builds its tracer during the auto-instrumentation
    bootstrap, long before any startup hook. Wrapping the shared multi-processor
    is what makes those pre-existing tracers pick the filter up.
    """
    provider, exporter = sdk_provider
    tracer = provider.get_tracer("built-before-install")

    assert install_span_export_filter() == 1

    with tracer.start_as_current_span("POST /chat/completions"):
        tracer.start_span("POST /chat/completions http send").end()
        span = tracer.start_span("postgresql", attributes={"db.statement": "BEGIN;"})
        span.end()
        tracer.start_span("postgresql", attributes={"db.statement": "SELECT 1"}).end()

    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["postgresql", "POST /chat/completions"]


def test_install_is_idempotent(sdk_provider):
    assert install_span_export_filter() == 1
    # Second call finds the processor already wrapped and wraps nothing new.
    assert install_span_export_filter() == 0


def test_install_respects_the_off_switch(monkeypatch, sdk_provider):
    monkeypatch.setenv("NANNOS_SPAN_FILTER", "off")
    assert install_span_export_filter() == 0


def test_install_is_a_noop_without_the_sdk():
    """No injected agent means the API's no-op provider; nothing to wrap."""
    previous = otel_trace.get_tracer_provider()
    otel_trace._TRACER_PROVIDER = otel_trace.NoOpTracerProvider()  # type: ignore[attr-defined]
    try:
        assert install_span_export_filter() == 0
    finally:
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]


def test_failed_request_chatter_still_reaches_the_exporter(sdk_provider):
    provider, exporter = sdk_provider
    tracer = provider.get_tracer("test")
    install_span_export_filter()

    span = tracer.start_span("POST /chat/completions http send")
    span.set_status(Status(StatusCode.ERROR, "connection reset"))
    span.end()

    assert [s.name for s in exporter.get_finished_spans()] == ["POST /chat/completions http send"]
