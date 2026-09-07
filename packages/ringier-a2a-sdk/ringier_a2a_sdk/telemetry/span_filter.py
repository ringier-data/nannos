"""Drop per-message telemetry chatter before it reaches the exporter.

WHY THIS EXISTS
---------------
A single chat turn produced 700-860 spans and about 1 MB of trace document.
Roughly 60% of that was chatter that says nothing a human or a service map can
use, and it buries the spans that matter (the turn root, LLM calls, tool calls,
DB queries). Measured across two real turns:

===========================  =======  =======
bucket                        turn 1   turn 2
===========================  =======  =======
ASGI ``http send``/``receive``   322      142
DB transaction control            64      210
===========================  =======  =======

The ASGI ones are the worse offender: the instrumentation opens one span per
ASGI message, so streaming one LLM response through the gateway costs a span
per forwarded chunk — 116 of them for a single completion.

WHAT SURVIVES
-------------
The HTTP *request* span is untouched. ``POST /chat/completions`` — with method,
route, status and duration — is a different span from the ``… http send`` /
``… http receive`` children, and only the children are dropped. Service maps,
latency and error rates are unaffected. Likewise, real SQL (SELECT, INSERT,
UPDATE, COMMIT that failed) is untouched; only successful transaction
bookkeeping goes.

WHY AT on_end AND NOT AT SAMPLING
---------------------------------
Dropping at span creation is the cheaper option and the one OTel designs for
(a Sampler), but it is not reachable here. A `Tracer` captures the sampler when
it is built, and every instrumentation builds its tracer during the injected
auto-instrumentation bootstrap — before any application startup hook runs. The
one object all those tracers *share* is the provider's active span processor, so
wrapping the processors inside it applies to tracers created before and after
install alike.

The cost of that choice: the spans are still created and still occupy the
export queue. This trims what reaches X-Ray, not what the process builds.

LEAF SPANS ONLY
---------------
A dropped span keeps its id in its children's ``parent_id``, which would leave
those children orphaned. Both categories here are leaves by construction — an
ASGI event span wraps one ``send``/``receive`` call, and a ``BEGIN`` has no
nested statement — so nothing is orphaned. Anything added to the drop rules
must be a leaf too.

Set ``NANNOS_SPAN_FILTER=off`` to disable without a code change.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

_DISABLE_ENV = "NANNOS_SPAN_FILTER"

# The ASGI instrumentation names these ``{parent span name} http send`` /
# ``… http receive``, so match the suffix rather than the whole name.
_ASGI_EVENT_SUFFIXES = (" http send", " http receive")

# Transaction bookkeeping, as it reaches us: ``BEGIN;``, ``COMMIT;``,
# ``ROLLBACK;`` and the bare ``;`` the pool emits when it checks a connection.
# Deliberately an exact set, not a prefix match — ``ROLLBACK TO SAVEPOINT`` or a
# statement that merely starts with one of these words stays.
_TRANSACTION_CONTROL = frozenset({"", "begin", "commit", "rollback"})

# Both spellings exist in the wild: db.statement is the older convention, and
# db.query.text is what the current semantic conventions use.
_STATEMENT_KEYS = ("db.statement", "db.query.text")


def _is_asgi_event_span(span: Any) -> bool:
    name = getattr(span, "name", None)
    return isinstance(name, str) and name.endswith(_ASGI_EVENT_SUFFIXES)


def _is_transaction_control_span(span: Any) -> bool:
    attributes = getattr(span, "attributes", None) or {}
    for key in _STATEMENT_KEYS:
        statement = attributes.get(key)
        if isinstance(statement, str):
            normalized = statement.strip().rstrip(";").strip().lower()
            return normalized in _TRANSACTION_CONTROL
    return False


def _carries_a_problem(span: Any) -> bool:
    """Keep anything that failed.

    A ``COMMIT`` that raised, or a send that broke mid-stream, is the one
    instance of these spans worth having. Events cover recorded exceptions.
    """
    status = getattr(span, "status", None)
    if status is not None and getattr(status, "status_code", None) is StatusCode.ERROR:
        return True
    return bool(getattr(span, "events", None))


def should_drop(span: Any) -> bool:
    """True when a span is pure chatter and safe to leave out of the export."""
    if _carries_a_problem(span):
        return False
    return _is_asgi_event_span(span) or _is_transaction_control_span(span)


class FilteringSpanProcessor:
    """Forwards everything except the spans ``should_drop`` rejects.

    Duck-typed rather than subclassing ``SpanProcessor``: this module is copied
    into the litellm-proxy image, where only the injected OTel agent provides
    the SDK, so importing an SDK type at module scope would break it.

    ``on_start`` still delegates. A processor may keep per-span state between
    start and end, and silently dropping the start half would corrupt it — the
    filtering belongs at ``on_end``, where the export decision is made.

    Everything not named here is forwarded by ``__getattr__``. The SDK's
    multi-processor calls private hooks on its children (``_on_ending`` in this
    version), and new ones arrive between releases; forwarding by default means
    a wrapped processor keeps working when one appears, instead of failing on
    every span end.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dropped = 0

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this class does not define. Fetched via
        # __getattribute__ so a lookup before __init__ finishes raises
        # AttributeError instead of recursing.
        try:
            inner = object.__getattribute__(self, "_inner")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(inner, name)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: Any) -> None:
        try:
            drop = should_drop(span)
        except Exception:  # noqa: BLE001 - never lose a span to a filter bug
            logger.debug("span filter raised; keeping span", exc_info=True)
            drop = False
        if drop:
            self.dropped += 1
            return
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def install_span_export_filter() -> int:
    """Wrap the active provider's span processors with the filter.

    Call once per process at startup. Returns how many processors were wrapped
    (0 when tracing is not active, the filter is switched off, or the provider
    is not the SDK one — all of which are normal, not failures).
    """
    if os.getenv(_DISABLE_ENV, "on").strip().lower() in {"off", "0", "false", "no"}:
        logger.info("span export filter disabled by %s", _DISABLE_ENV)
        return 0

    # Imported lazily: with no injected agent (local dev, tests) the API's no-op
    # provider has none of these attributes and there is nothing to wrap.
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    multi = getattr(provider, "_active_span_processor", None)
    processors = getattr(multi, "_span_processors", None)
    if not isinstance(processors, tuple):
        logger.debug("no SDK span processors found; span export filter not installed")
        return 0

    wrapped = 0

    def wrap(processor: Any) -> Any:
        nonlocal wrapped
        if isinstance(processor, FilteringSpanProcessor):
            return processor
        wrapped += 1
        return FilteringSpanProcessor(processor)

    # The multi-processor guards its tuple with a lock that add_span_processor
    # also takes; hold it so a processor registered concurrently is not lost.
    lock = getattr(multi, "_lock", None)
    if lock is not None:
        with lock:
            multi._span_processors = tuple(wrap(p) for p in processors)
    else:
        multi._span_processors = tuple(wrap(p) for p in processors)

    if wrapped:
        logger.info(
            "span export filter installed on %d processor(s): dropping ASGI "
            "send/receive events and successful DB transaction control",
            wrapped,
        )
    return wrapped


__all__ = [
    "FilteringSpanProcessor",
    "install_span_export_filter",
    "should_drop",
]
