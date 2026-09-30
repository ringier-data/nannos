"""The request shapes the Nannos harness sends, and what a model was seen to accept.

WHY THIS EXISTS
---------------
A model's registration test used to be a four-token ping. It passed for models that then
broke the first real turn: some reject a forced ``tool_choice``, some reject
``response_format``, some reject ``thinking: disabled`` and want ``between_tools`` instead,
and provider capability maps say nothing reliable about any of it. The only authority is a
live call with the exact shape the harness will send — so the shapes are written down ONCE,
here, and the registration probe replays them.

Three consumers read this module, and none of them may drift from the others:

* **console-backend** runs ``probe_model`` at registration and stores the result under
  ``model_info[CAPABILITIES_KEY]`` on the gateway deployment.
* **the gateway hook** (litellm-proxy ``custom_logger.py``) reads the stored flags off the
  deployment that actually serves a request — under failover that is not the alias the app
  asked for — and rewrites the request into a shape that deployment accepts
  (``downgrade_forced_tool_choice``, ``thinking_off_switch``).
* **agent-common** reads the same flags for the alias it is about to call and picks the
  non-forced structured-output path deliberately when forcing is known to fail.

The module is copied into the proxy image as a single file (see the litellm-proxy
Dockerfile), so it must stay free of intra-package imports and third-party dependencies.

ROUTABLE VERSUS UNAVOIDABLE
---------------------------
Each shape says what happens when a model rejects it. *Unavoidable* shapes are the ones every
agent turn sends and the harness has no alternative for: a model that fails one cannot run a
turn, so registration is refused. *Routable* shapes have a second form the harness can pick
once it knows the limitation — a forced ``tool_choice`` becomes ``auto`` plus the prompt's
instruction, ``thinking: disabled`` becomes ``between_tools``, a forced structured-output tool
becomes an ordinary bound tool, a replayed thinking block is stripped by the hook. Those are
recorded, not refused: the record is exactly what the hook and the app need in order to route
around them. A transient failure records nothing (see ``probe_model``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: model_info key under which the probe's findings are stored on a gateway deployment.
CAPABILITIES_KEY = "nannos_capabilities"

#: Request ``metadata`` key that marks a probe request. The gateway hook skips every
#: record-driven rewrite for a marked request, so a re-probe measures the model and not the
#: hook (otherwise a deployment recorded as unable to force a tool call would have its forced
#: probe downgraded to ``auto``, pass, and flip its own record). The proxy keeps the caller's
#: ``metadata`` dict on the request the router sees, merged with its own fields.
PROBE_MARKER = "nannos_probe"

#: Wall-clock budget for one whole probe. Shapes not reached in time are inconclusive.
DEFAULT_BUDGET_SECONDS = 180.0

# --- the flags ---------------------------------------------------------------------------
#: bool — the deployment accepts ``tool_choice: required`` and a named ``tool_choice``.
FORCED_TOOL_CHOICE = "forced_tool_choice"
#: bool — the deployment accepts ``response_format: {type: json_schema}``.
RESPONSE_FORMAT = "response_format"
#: str — which explicit ``thinking`` value turns thinking off next to ``reasoning_effort: none``
#: and tools: ``"disabled"``, ``"between_tools"`` or ``"none"`` (no explicit switch is accepted;
#: send ``reasoning_effort: none`` alone and let the provider do what it does).
THINKING_OFF = "thinking_off"
#: bool | None — a signed thinking block from the model can be replayed with its tool result.
#: ``None`` when the model returned no thinking block to replay (nothing to record).
THINKING_REPLAY = "thinking_replay"
#: ISO-8601 timestamp of the probe that wrote the flags.
PROBED_AT = "probed_at"

#: Which record key each routable probe shape decides. Whoever merges a partial probe over a
#: stored record needs this to keep exactly the keys of the shapes that were inconclusive.
SHAPE_KEYS: dict[str, str] = {
    "forced_tool_choice": FORCED_TOOL_CHOICE,
    "named_tool_choice": FORCED_TOOL_CHOICE,
    "response_format": RESPONSE_FORMAT,
    "thinking_off": THINKING_OFF,
    "thinking_replay": THINKING_REPLAY,
}

THINKING_OFF_DISABLED = "disabled"
THINKING_OFF_BETWEEN_TOOLS = "between_tools"
THINKING_OFF_NONE = "none"

#: The one small tool every probe binds. Deliberately boring: the point is the wire shape,
#: not the answer.
PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

#: Schema for the response_format shape — the same size as the harness's classifier outputs.
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}

_ASK_TOOL = "What is the weather in Zurich right now? Use the get_weather tool to find out."
_MAX_TOKENS = 64


class ProbeCallError(Exception):
    """The gateway (or the provider behind it) rejected a probe request.

    ``status`` is the HTTP status when there was one; ``message`` is the provider's reason,
    trimmed — it is shown to the admin, so callers must only raise it for requests that carry
    no credentials (every probe request is one: the alias is already registered).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status

    @property
    def transient(self) -> bool:
        """A failure that says nothing about the model: rate limit, timeout, a 5xx, the
        gateway unreachable (``status`` None), or a cooled-down deployment. Such a shape is
        *inconclusive* and leaves no record, instead of being written down as a limitation."""
        return is_transient_status(self.status)


def is_transient_status(status: int | None) -> bool:
    return status is None or status in (408, 429) or status >= 500


#: ``call(body) -> response``. For a non-streaming body the response is the parsed JSON dict;
#: for a body with ``"stream": True`` it is the raw SSE text, which ``probe_model`` assembles.
ProbeCall = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ShapeResult:
    """One shape's outcome."""

    shape: str
    ok: bool
    #: Provider reason on failure; empty on success.
    error: str = ""
    #: Whether a failure here refuses registration (True) or is recorded as a flag (False).
    unavoidable: bool = False
    #: Free-form observations the admin may want ("0 reasoning tokens", "no thinking block").
    note: str = ""
    #: The shape could not be measured (transient failure or out of budget): no verdict, no
    #: record. Never counts as a limitation or a rejection.
    inconclusive: bool = False


@dataclass
class ProbeReport:
    """What the probe saw. ``rejected`` names the unavoidable shapes that failed, if any;
    ``capabilities`` is the flag dict to store under ``CAPABILITIES_KEY``."""

    model: str
    results: list[ShapeResult] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> list[ShapeResult]:
        return [r for r in self.results if r.unavoidable and not r.ok and not r.inconclusive]

    @property
    def limitations(self) -> list[ShapeResult]:
        return [r for r in self.results if not r.unavoidable and not r.ok and not r.inconclusive]

    @property
    def inconclusive(self) -> list[ShapeResult]:
        return [r for r in self.results if r.inconclusive]

    @property
    def inconclusive_keys(self) -> set[str]:
        """Record keys whose shape was attempted but could not be measured: a stored value for
        them must survive this probe's write."""
        return {SHAPE_KEYS[r.shape] for r in self.inconclusive if r.shape in SHAPE_KEYS}

    @property
    def inconclusive_unavoidable(self) -> list[ShapeResult]:
        """Unavoidable shapes that could not be measured: the model may be fine, but nothing
        says so — registration cannot be confirmed, and must not be refused either."""
        return [r for r in self.inconclusive if r.unavoidable]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "capabilities": dict(self.capabilities),
            "rejected": [r.shape for r in self.rejected],
            "inconclusive": [r.shape for r in self.inconclusive],
            "results": [
                {
                    "shape": r.shape,
                    "ok": r.ok,
                    "error": r.error,
                    "unavoidable": r.unavoidable,
                    "note": r.note,
                    "inconclusive": r.inconclusive,
                }
                for r in self.results
            ],
        }


# --- request builders ---------------------------------------------------------------------
# Each returns the body the harness would send. Kept as plain dicts so the probe, the tests
# and a human reading a gateway log see the same thing.


def _base(model: str, **extra: Any) -> dict[str, Any]:
    # `disable_fallbacks`: the verdict is about THIS alias's deployment, so a tier-group chain
    # must not answer a rejected shape from the next alias. The marker lets the hook tell probe
    # traffic from the app's (see PROBE_MARKER).
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "disable_fallbacks": True,
        "metadata": {PROBE_MARKER: True},
    }
    body.update(extra)
    return body


def shape_tools_auto(model: str) -> dict[str, Any]:
    """Every agent turn: tools bound, the model decides (``tool_choice: auto``)."""
    return _base(
        model,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
    )


def shape_tool_round_trip(model: str) -> dict[str, Any]:
    """Every agent turn: an assistant ``tool_calls`` message followed by its ``role: tool``
    result. The call is synthetic so the shape is exercised even by a model that would not
    have called the tool on its own."""
    return _base(
        model,
        messages=[
            {"role": "user", "content": _ASK_TOOL},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_probe_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": json.dumps({"city": "Zurich"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_probe_1", "content": '{"temperature_c": 18, "sky": "clear"}'},
        ],
        tools=[PROBE_TOOL],
        tool_choice="auto",
    )


def shape_streaming_tools(model: str) -> dict[str, Any]:
    """Every agent turn streams, with tools bound and usage in the final chunk."""
    return _base(
        model,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        stream=True,
        stream_options={"include_usage": True},
    )


def shape_forced_tool_choice(model: str) -> dict[str, Any]:
    """``ToolStrategy`` with thinking off: langchain binds the schema tool with
    ``tool_choice: any``, which the OpenAI client sends as ``required``."""
    return _base(
        model,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="required",
    )


def shape_named_tool_choice(model: str) -> dict[str, Any]:
    """``with_structured_output(method="function_calling")`` (the tool risk scorer): one tool,
    named ``tool_choice``, ``parallel_tool_calls: false``."""
    return _base(
        model,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
        parallel_tool_calls=False,
    )


def shape_response_format(model: str) -> dict[str, Any]:
    """``with_structured_output()``'s default: ``response_format: json_schema``, no tools —
    the HITL reply classifier, tool-call summaries, toolset selection, file filtering."""
    return _base(
        model,
        messages=[{"role": "user", "content": "Is Zurich in Switzerland? Answer with your confidence."}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "probe_answer", "schema": PROBE_SCHEMA, "strict": False},
        },
    )


def shape_thinking_off(model: str, switch: str) -> dict[str, Any]:
    """The fast model, summaries and classifiers: ``reasoning_effort: none`` with tools. The
    explicit ``thinking`` value is what the gateway hook adds per deployment; the probe sends
    it itself to learn which one the deployment takes, and the hook leaves it alone because
    the request carries the probe marker (any other caller's value is replaced)."""
    return _base(
        model,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        reasoning_effort="none",
        thinking={"type": switch},
    )


def shape_thinking_on(model: str) -> dict[str, Any]:
    """A thinking turn with tools — the first half of the replay shape."""
    return _base(
        model,
        max_tokens=2048,
        messages=[{"role": "user", "content": _ASK_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        reasoning_effort="low",
    )


def shape_thinking_replay(model: str, assistant: dict[str, Any]) -> dict[str, Any]:
    """The second half: the model's own signed thinking block and tool call, replayed with the
    tool result — what the app does on every thinking turn on Anthropic-family providers."""
    tool_calls = assistant.get("tool_calls") or []
    call_id = (tool_calls[0].get("id") if tool_calls else None) or "call_probe_1"
    replayed: dict[str, Any] = {
        "role": "assistant",
        "content": assistant.get("content"),
        "thinking_blocks": assistant.get("thinking_blocks"),
    }
    if tool_calls:
        replayed["tool_calls"] = tool_calls
    else:
        replayed["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": "Zurich"})},
            }
        ]
    return _base(
        model,
        max_tokens=2048,
        messages=[
            {"role": "user", "content": _ASK_TOOL},
            replayed,
            {"role": "tool", "tool_call_id": call_id, "content": '{"temperature_c": 18, "sky": "clear"}'},
        ],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        reasoning_effort="low",
    )


# --- response helpers ---------------------------------------------------------------------


def assemble_stream(sse_text: str) -> dict[str, Any]:
    """Fold an SSE chat-completions stream into one non-streaming-shaped response: the
    concatenated content, the tool calls (arguments joined by index) and the usage from the
    final chunk. Raises ``ProbeCallError`` when the stream carries an error event."""
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    for raw in sse_text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if isinstance(chunk, dict) and "error" in chunk:
            err = chunk["error"]
            raise ProbeCallError(str(err.get("message") if isinstance(err, dict) else err))
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage or {}}


def _message(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    choices = response.get("choices") or []
    if not choices:
        return {}
    return (choices[0] or {}).get("message") or {}


def _reasoning_tokens(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    details = (response.get("usage") or {}).get("completion_tokens_details") or {}
    value = details.get("reasoning_tokens")
    return int(value) if isinstance(value, (int, float)) else None


# --- the probe -----------------------------------------------------------------------------


async def probe_model(
    model: str,
    call: ProbeCall,
    *,
    supports_reasoning: bool = False,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> ProbeReport:
    """Replay every harness shape against ``model`` through ``call`` and report.

    The unavoidable shapes run first; if any fails the routable ones still run, so the admin
    sees the whole picture in one go. ``supports_reasoning`` (the admin's declaration on the
    deployment) gates the thinking shapes: a model registered without thinking never gets a
    thinking request from the harness, so there is nothing to learn.

    Only a definite provider rejection (a 4xx other than 408/429) is a verdict. A transient
    failure, or a shape not reached within ``budget_seconds``, is *inconclusive*: it is reported
    but writes no flag, and it never refuses a model. ``report.capabilities`` therefore holds
    exactly the keys that were measured.
    """
    report = ProbeReport(model=model)
    started = time.monotonic()

    async def attempt(body: dict[str, Any]) -> tuple[bool, Any, str, bool]:
        """Send one shape; (ok, response, provider reason, transient)."""
        if time.monotonic() - started > budget_seconds:
            return False, None, f"probe budget of {budget_seconds:.0f}s exhausted before this shape", True
        try:
            response = await call(body)
            if body.get("stream"):
                response = assemble_stream(response if isinstance(response, str) else "")
            return True, response, "", False
        except ProbeCallError as e:
            return False, None, e.message, e.transient
        except Exception as e:  # noqa: BLE001 — a transport failure is inconclusive, not a crash
            return False, None, f"{type(e).__name__}: {e}", True

    # Unavoidable ----------------------------------------------------------------------
    for shape, body in (
        ("tools_auto", shape_tools_auto(model)),
        ("tool_round_trip", shape_tool_round_trip(model)),
        ("streaming_tools", shape_streaming_tools(model)),
    ):
        ok, _, err, transient = await attempt(body)
        report.results.append(ShapeResult(shape, ok, err, unavoidable=True, inconclusive=not ok and transient))

    # Forced tool choice: both forms must work for the flag to be True ------------------
    forced_ok, _, forced_err, forced_t = await attempt(shape_forced_tool_choice(model))
    report.results.append(ShapeResult("forced_tool_choice", forced_ok, forced_err, inconclusive=not forced_ok and forced_t))
    named_ok, _, named_err, named_t = await attempt(shape_named_tool_choice(model))
    report.results.append(ShapeResult("named_tool_choice", named_ok, named_err, inconclusive=not named_ok and named_t))
    if forced_ok and named_ok:
        report.capabilities[FORCED_TOOL_CHOICE] = True
    elif (not forced_ok and not forced_t) or (not named_ok and not named_t):
        report.capabilities[FORCED_TOOL_CHOICE] = False  # a definite rejection of either form

    # response_format ------------------------------------------------------------------
    rf_ok, _, rf_err, rf_t = await attempt(shape_response_format(model))
    report.results.append(ShapeResult("response_format", rf_ok, rf_err, inconclusive=not rf_ok and rf_t))
    if rf_ok or not rf_t:
        report.capabilities[RESPONSE_FORMAT] = rf_ok

    # Thinking off: the first explicit switch that works, else none ----------------------
    off_switch: str | None = THINKING_OFF_NONE
    off_note = ""
    for switch in (THINKING_OFF_DISABLED, THINKING_OFF_BETWEEN_TOOLS):
        ok, response, err, transient = await attempt(shape_thinking_off(model, switch))
        if ok:
            off_switch = switch
            rt = _reasoning_tokens(response)
            off_note = f"{rt} reasoning tokens" if rt is not None else ""
            break
        if transient:
            off_switch = None  # cannot tell which switch works; leave no record
            off_note = err
            break
        off_note = err
    if off_switch is None:
        report.results.append(ShapeResult("thinking_off", False, off_note, inconclusive=True))
    else:
        conclusive_ok = off_switch != THINKING_OFF_NONE
        report.results.append(
            ShapeResult(
                "thinking_off",
                conclusive_ok,
                "" if conclusive_ok else f"no explicit switch accepted; last error: {off_note}",
                note=off_note if conclusive_ok else "",
            )
        )
        report.capabilities[THINKING_OFF] = off_switch

    # Thinking replay: only for models declared to think ---------------------------------
    if supports_reasoning:
        on_ok, response, on_err, on_t = await attempt(shape_thinking_on(model))
        assistant = _message(response) if on_ok else {}
        if on_ok and assistant.get("thinking_blocks"):
            ok, _, err, transient = await attempt(shape_thinking_replay(model, assistant))
            report.results.append(ShapeResult("thinking_replay", ok, err, inconclusive=not ok and transient))
            if ok or not transient:
                report.capabilities[THINKING_REPLAY] = ok
        elif on_ok:
            report.results.append(ShapeResult("thinking_replay", True, note="no thinking block returned; nothing to replay"))
            report.capabilities[THINKING_REPLAY] = None
        else:
            report.results.append(
                ShapeResult("thinking_replay", False, f"thinking turn failed: {on_err}", inconclusive=on_t)
            )
            if not on_t:
                report.capabilities[THINKING_REPLAY] = False

    return report


# --- hook-side helpers ----------------------------------------------------------------------
# Read by the gateway's pre-call deployment hook with the SERVING deployment's model_info in
# hand, and by agent-common with the requested alias's model_info. Every helper tolerates an
# unprobed deployment (no flags) by returning "no opinion".


def is_probe_request(kwargs: dict[str, Any]) -> bool:
    """Whether a request the hook sees is registration-probe traffic (see PROBE_MARKER). The
    proxy keeps the caller's ``metadata`` under ``metadata`` (chat completions) or
    ``litellm_metadata`` (the assistants-style routes); both are checked."""
    for key in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(key)
        if isinstance(bucket, dict) and bucket.get(PROBE_MARKER):
            return True
    return False


def capabilities_of(model_info: Any) -> dict[str, Any]:
    """The probe's flags from a deployment's ``model_info``, or ``{}`` when never probed."""
    if not isinstance(model_info, dict):
        return {}
    caps = model_info.get(CAPABILITIES_KEY)
    return caps if isinstance(caps, dict) else {}


def _is_forced(tool_choice: Any) -> bool:
    if isinstance(tool_choice, str):
        return tool_choice in ("required", "any")
    if isinstance(tool_choice, dict):
        return (tool_choice.get("type") in ("function", "tool")) or "function" in tool_choice
    return False


def downgrade_forced_tool_choice(kwargs: dict[str, Any], caps: dict[str, Any]) -> bool:
    """Turn a forced ``tool_choice`` into ``auto`` when the deployment is recorded as unable
    to force one. True when ``kwargs`` changed. Top-level keys only: each attempt gets its own
    shallow copy from the router, so the change never leaks into a fallback attempt."""
    if caps.get(FORCED_TOOL_CHOICE) is not False:
        return False
    if not _is_forced(kwargs.get("tool_choice")):
        return False
    kwargs["tool_choice"] = "auto"
    # A named tool_choice travels with parallel_tool_calls=False on the risk-scorer path; the
    # pair is fine on its own, so it is left as sent.
    return True


def thinking_off_switch(caps: dict[str, Any]) -> dict[str, str] | None:
    """The explicit ``thinking`` value to send for thinking-off on a probed deployment:
    ``{"type": "disabled"}``, ``{"type": "between_tools"}`` or ``None`` (send no ``thinking``,
    the provider default for ``reasoning_effort: none`` is the best available). Returns
    ``None`` also when unprobed — callers fall back to their own heuristic then, so tell the
    two apart with ``THINKING_OFF in caps``."""
    switch = caps.get(THINKING_OFF)
    if switch in (THINKING_OFF_DISABLED, THINKING_OFF_BETWEEN_TOOLS):
        return {"type": switch}
    return None
