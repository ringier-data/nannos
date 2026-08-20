"""Per-test token accounting via a LangChain callback.

Aggregation is langchain-core's ``UsageMetadataCallbackHandler``, not a
reimplementation: provider usage shapes change, and upstream tracks them. It also
holds a lock, which matters here because in-process sub-agent calls inherit this
config and can overlap.

Deliberately *not* ``CostTrackingCallback`` from the SDK: that needs a live
``CostLogger`` with a background worker POSTing to console-backend, which is a lot
of infrastructure for a number we only want to print.

This subclass adds two things upstream does not owe us:

*A flat total.* Upstream keys usage by model name, which is the right shape for a
report but not for ``TestRecord.input_tokens``. The properties below sum it.

*A fallback, and evidence about whether it is needed.* Upstream records only when
the response carries **both** ``usage_metadata`` and a ``model_name`` in
``response_metadata`` — miss either and the call silently contributes zero, which
in a cost report is indistinguishable from a free call. Gateway models are built
with ``stream_usage=True`` (``agent_common.core.model_factory``) so usage should
always be present, but "should" is doing work there and a silent zero is the one
failure mode this module exists to prevent. So anything upstream declines is read
here instead and counted in ``unattributed_calls``.

If a full real-tier run reports ``unattributed_calls == 0``, this fallback is dead
and can go — with evidence rather than by assumption.

Attached through the graph config (``config["callbacks"]``), which LangChain
propagates down to the model call, so no production code needs a test hook.

Token counts remain best-effort: a zero means "not reported", not "free". A remote
A2A sub-agent's usage is billed in its own process and never appears here.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.outputs import LLMResult


class UsageRecorder(UsageMetadataCallbackHandler):
    """Upstream per-model aggregation, plus flat totals and a counted fallback."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.unattributed_calls = 0
        self._extra_input = 0
        self._extra_output = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.calls += 1

        # Snapshot rather than re-deriving upstream's accept/reject condition: a
        # copy of the condition would drift from it, and this cannot double count —
        # if the dict moved, upstream owns the call and we add nothing.
        before = dict(self.usage_metadata)
        super().on_llm_end(response, **kwargs)
        if self.usage_metadata != before:
            return

        self.unattributed_calls += 1
        self._record_unattributed(response)

    def _record_unattributed(self, response: LLMResult) -> None:
        """Read usage upstream declined, preferring the message to ``llm_output``."""
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    # Present, but unattributed — no model_name in response_metadata.
                    self._extra_input += int(usage.get("input_tokens") or 0)
                    self._extra_output += int(usage.get("output_tokens") or 0)
                    return

        # The provider-level block, spelled differently across providers.
        output = response.llm_output or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        if usage:
            self._extra_input += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            self._extra_output += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    @property
    def input_tokens(self) -> int:
        attributed = sum(int(u.get("input_tokens") or 0) for u in self.usage_metadata.values())
        return attributed + self._extra_input

    @property
    def output_tokens(self) -> int:
        attributed = sum(int(u.get("output_tokens") or 0) for u in self.usage_metadata.values())
        return attributed + self._extra_output

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"UsageRecorder(calls={self.calls}, in={self.input_tokens}, "
            f"out={self.output_tokens}, unattributed={self.unattributed_calls})"
        )
