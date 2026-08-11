"""Per-test token accounting via a LangChain callback.

Deliberately *not* ``CostTrackingCallback`` from the SDK: that needs a live
``CostLogger`` with a background worker POSTing to console-backend, which is a
lot of infrastructure for a number we only want to print. This handler just sums
what the provider reports and keeps it in memory.

Attached through the graph config (``config["callbacks"]``), which LangChain
propagates down to the model call, so no production code needs a test hook.

Token counts are best-effort. ``usage_metadata`` is the modern field and most
gateway responses carry it, but coverage varies by provider and streaming mode —
a zero means "not reported", not "free". Sub-agent LLM calls are counted too
when they run in-process and inherit the config; a remote A2A sub-agent's usage
is billed in its own process and will not appear here.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class UsageRecorder(BaseCallbackHandler):
    """Accumulates input/output token counts across every LLM call in a test."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.calls += 1

        # Preferred: usage_metadata on the message itself.
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    self.input_tokens += int(usage.get("input_tokens") or 0)
                    self.output_tokens += int(usage.get("output_tokens") or 0)
                    return

        # Fallback: the provider-level token_usage block, shaped differently
        # across providers, hence the several key spellings.
        output = response.llm_output or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        if usage:
            self.input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"UsageRecorder(calls={self.calls}, in={self.input_tokens}, out={self.output_tokens})"
