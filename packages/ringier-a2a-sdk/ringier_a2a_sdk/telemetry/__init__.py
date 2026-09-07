"""Telemetry helpers shared by every Nannos Python service.

Kept in this SDK rather than agent-common because console-backend and the
litellm-proxy image need them too, and neither depends on the LangGraph stack.
``span_filter`` in particular is written to be importable as a standalone file
(no intra-package imports), so the proxy image can copy just that one module.
"""

from .span_filter import install_span_export_filter, should_drop

__all__ = ["install_span_export_filter", "should_drop"]
