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
  (``downgrade_forced_tool_choice``, ``apply_thinking_off``).
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

import inspect
import json
import time
from collections.abc import Awaitable, Callable, Sequence
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
#: str — how thinking goes off next to ``reasoning_effort: none`` and tools: an explicit
#: ``thinking`` value (``"disabled"``, ``"between_tools"``), ``"none"`` (no explicit switch is
#: taken; ``reasoning_effort: none`` alone turns it off), or ``"always_on"`` (nothing turns it
#: off, so the console offers no thinking-off for the model and the gateway sends a thinking-off
#: request as THINKING_FLOOR), or ``"unsupported"`` (every off request was refused; see
#: THINKING_OFF_UNSUPPORTED).
THINKING_OFF = "thinking_off"
#: str — for an ``always_on`` deployment, the lowest ``reasoning_effort`` it accepted
#: (``"minimal"`` or ``"low"``). The gateway sends a thinking-off request as this effort.
#: ``reasoning_effort: none`` alone is not a floor everywhere: on Claude, LiteLLM turns it into
#: no thinking parameter and no effort, so the model runs at its DEFAULT effort with its thinking
#: text omitted (nannos#330). Absent when no level was accepted (or never measured): the gateway
#: then sends ``none`` alone, as before the key existed.
THINKING_FLOOR = "thinking_floor"
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
    "thinking_floor": THINKING_FLOOR,
    "thinking_replay": THINKING_REPLAY,
}

#: The efforts tried as the floor of an always-on deployment, lowest first, when the deployment
#: declares no levels of its own (see ``probe_model``'s ``floor_candidates``).
THINKING_FLOOR_CANDIDATES: tuple[str, ...] = ("minimal", "low")
#: How many of a deployment's declared levels, lowest first, are tried as its floor.
FLOOR_CANDIDATE_COUNT = 2

THINKING_OFF_DISABLED = "disabled"
THINKING_OFF_BETWEEN_TOOLS = "between_tools"
THINKING_OFF_NONE = "none"
THINKING_OFF_ALWAYS_ON = "always_on"
#: Every way of asking for thinking off was refused outright — the explicit switches AND
#: ``reasoning_effort: none`` alone (OpenAI-direct gpt-5 / o-series 400 on ``none``). Distinct
#: from ``none``, which means the effort alone *works*: here the gateway strips the effort and
#: the switch from a thinking-off request, so it goes out as a plain request with the
#: provider's default — the only shape this deployment was seen to accept.
THINKING_OFF_UNSUPPORTED = "unsupported"

#: What the admin sees for each shape while the probe runs and in its report. Order is the
#: order the probe sends them; ``thinking_replay`` only runs for a model declared to think.
SHAPE_LABELS: dict[str, str] = {
    "tools_auto": "Tools, model decides",
    "tool_round_trip": "Tool result round-trip",
    "streaming_tools": "Streaming with tools",
    "forced_tool_choice": "Forced tool call",
    "named_tool_choice": "Named tool call",
    "response_format": "Structured output (response_format)",
    "thinking_off": "Thinking off",
    "thinking_floor": "Lowest thinking level",
    "thinking_replay": "Thinking replay",
}

#: The individual requests inside a shape, for the progress line under the running shape.
STEP_LABELS: dict[str, str] = {
    "thinking_off:disabled": "trying thinking: disabled",
    "thinking_off:between_tools": "trying thinking: between_tools",
    "thinking_off:effort_only": "trying reasoning_effort: none alone",
    "thinking_off:control": "control turn with thinking on",
    **{
        f"thinking_floor:{effort}": f"trying reasoning_effort: {effort}"
        for effort in ("minimal", "low", "medium", "high", "xhigh", "max")
    },
    "thinking_replay:turn": "thinking turn",
    "thinking_replay:replay": "replaying the signed thinking block",
}


def planned_shapes(*, supports_reasoning: bool) -> list[str]:
    """The shapes ``probe_model`` will report, in order — for a progress display to lay out
    before the first result arrives."""
    return [s for s in SHAPE_LABELS if s != "thinking_replay" or supports_reasoning]


#: ``on_progress(event)``: ``{"type": "step", "shape", "step", "label"}`` right before each
#: request, ``{"type": "result", **ShapeResult}`` when a shape's verdict is in. Awaited when it
#: returns an awaitable; an exception in it is logged by nobody and ignored — progress must
#: never change a verdict.
ProbeProgress = Callable[[dict[str, Any]], Any]

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
#: The forced shapes ask for NO tool call, so only an honoured force produces one: under
#: ``auto`` the model would follow the prompt, and a gateway that silently rewrote the force
#: to ``auto`` shows up as a reply without a tool call.
_FORBID_TOOL = "Reply with the single word OK. Do not call any tool."
#: A question the model reasons about when thinking is on, so a thinking-off switch that was
#: dropped on the way shows up as reasoning in the reply. It has to be hard enough that an
#: adaptive-thinking model does not answer it outright: "how many primes between 100 and 200"
#: was answered in 3 tokens at reasoning_effort medium by Sonnet 5.5, which verifies nothing.
_THINK = "Think it through carefully: what is 48271 * 69621 - 1234567? Reply with only the number."
_MAX_TOKENS = 64
#: Graded shapes need room for the answer the grade reads; a model that thinks regardless
#: (adaptive thinking) must not be cut off before it gets there.
_GRADED_MAX_TOKENS = 1024


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


#: What LiteLLM's router answers once it has cooled a deployment down (after a 404, a 401, a
#: rate limit…): no provider was asked, so it says nothing about the shape.
_COOLDOWN_MARKERS = ("No deployments available", "cooldown_list")
_NOT_PROBED_COOLDOWN = (
    "not probed — the gateway took this deployment out of rotation after an earlier failure; "
    "fix that failure and re-run Test"
)


def _is_cooldown(message: str) -> bool:
    return any(m in message for m in _COOLDOWN_MARKERS)


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
            "results": [_result_dict(r) for r in self.results],
        }


def _result_dict(r: ShapeResult) -> dict[str, Any]:
    return {
        "shape": r.shape,
        "label": SHAPE_LABELS.get(r.shape, r.shape),
        "ok": r.ok,
        "error": r.error,
        "unavoidable": r.unavoidable,
        "note": r.note,
        "inconclusive": r.inconclusive,
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
        max_tokens=_GRADED_MAX_TOKENS,
        messages=[{"role": "user", "content": _FORBID_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice="required",
    )


def shape_named_tool_choice(model: str) -> dict[str, Any]:
    """``with_structured_output(method="function_calling")`` (the tool risk scorer): one tool,
    named ``tool_choice``, ``parallel_tool_calls: false``."""
    return _base(
        model,
        max_tokens=_GRADED_MAX_TOKENS,
        messages=[{"role": "user", "content": _FORBID_TOOL}],
        tools=[PROBE_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
        parallel_tool_calls=False,
    )


def shape_response_format(model: str) -> dict[str, Any]:
    """``with_structured_output()``'s default: ``response_format: json_schema``, no tools —
    the HITL reply classifier, tool-call summaries, toolset selection, file filtering."""
    return _base(
        model,
        max_tokens=_GRADED_MAX_TOKENS,
        messages=[{"role": "user", "content": "Is Zurich in Switzerland? Answer with your confidence."}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "probe_answer", "schema": PROBE_SCHEMA, "strict": False},
        },
    )


def shape_thinking_off(model: str, switch: str | None) -> dict[str, Any]:
    """Every thinking-off call (the fast model, summaries, a turn with Extended Thinking off):
    ``reasoning_effort: none`` with tools. The explicit ``thinking`` value is what the gateway
    hook adds per deployment; the probe sends it itself to learn which one the deployment
    takes, and the hook leaves it alone because the request carries the probe marker (any
    other caller's value is replaced). ``switch=None`` sends the effort alone."""
    body = _base(
        model,
        max_tokens=_GRADED_MAX_TOKENS,
        messages=[{"role": "user", "content": _THINK}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        reasoning_effort="none",
    )
    if switch is not None:
        body["thinking"] = {"type": switch}
    return body


def shape_thinking_floor(model: str, effort: str) -> dict[str, Any]:
    """A thinking-off call as the gateway sends it to an always-on deployment: the thinking-off
    question at a real, low ``effort`` instead of ``none``."""
    body = shape_thinking_off(model, None)
    body["reasoning_effort"] = effort
    return body


def shape_thinking_control(model: str) -> dict[str, Any]:
    """The thinking-off question with thinking ON: shows the question makes this model reason
    at all, so a reply without reasoning under a switch means the switch worked."""
    return _base(
        model,
        # Above the reasoning budget LiteLLM maps ``high`` to on budget-thinking Claude (4096):
        # at or below it every control turn 400s and every switch reads "unverified".
        max_tokens=8192,
        messages=[{"role": "user", "content": _THINK}],
        tools=[PROBE_TOOL],
        tool_choice="auto",
        reasoning_effort="high",
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


def _truncated(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    choices = response.get("choices") or []
    return bool(choices) and (choices[0] or {}).get("finish_reason") == "length"


def _tool_call_names(response: Any) -> list[str]:
    return [((tc or {}).get("function") or {}).get("name") or "" for tc in _message(response).get("tool_calls") or []]


def _reasoned(response: Any) -> bool:
    """Whether the reply shows the model reasoned: reasoning tokens billed, or reasoning text or
    thinking blocks returned (providers report one, the other, or both)."""
    message = _message(response)
    return bool((_reasoning_tokens(response) or 0) > 0 or message.get("reasoning_content") or message.get("thinking_blocks"))


def _matches_probe_schema(response: Any) -> bool:
    content = _message(response).get("content")
    try:
        parsed = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return False
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("answer"), str)
        and isinstance(parsed.get("confidence"), (int, float))
    )


#: Why a 200 can still be a rejection: the gateway may rewrite a shape it knows the model
#: refuses (LiteLLM's ``drop_params`` downgrades a forced ``tool_choice`` and drops
#: ``thinking: disabled`` for models its map flags) and answer the rewritten request.
_REWRITTEN = "accepted, but {what}: the gateway may have rewritten the request"


def _check_forced(response: Any) -> str:
    return "" if _tool_call_names(response) else _REWRITTEN.format(what="the reply made no tool call")


def _check_named(response: Any) -> str:
    names = _tool_call_names(response)
    if PROBE_TOOL["function"]["name"] in names:
        return ""
    return _REWRITTEN.format(what=f"the reply called {names} instead of the named tool" if names else "the reply made no tool call")


def _check_response_format(response: Any) -> str:
    return "" if _matches_probe_schema(response) else _REWRITTEN.format(what="the reply is not JSON matching the schema")


# --- the probe -----------------------------------------------------------------------------


async def probe_model(
    model: str,
    call: ProbeCall,
    *,
    supports_reasoning: bool = False,
    floor_candidates: Sequence[str] | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    on_progress: ProbeProgress | None = None,
) -> ProbeReport:
    """Replay every harness shape against ``model`` through ``call`` and report.

    The unavoidable shapes run first; if any fails the routable ones still run, so the admin
    sees the whole picture in one go. ``supports_reasoning`` (the admin's declaration on the
    deployment) gates the thinking shapes: a model registered without thinking never gets a
    thinking request from the harness, so there is nothing to learn.
    ``floor_candidates`` are the levels the deployment declares (lowest first) — the console
    passes the ones its picker offers; the first ``FLOOR_CANDIDATE_COUNT`` are tried as the
    always-on floor, and ``THINKING_FLOOR_CANDIDATES`` when none are given.

    A verdict is either a definite provider rejection (a 4xx other than 408/429) or, for a
    routable shape, a reply that shows the shape was not honoured — a 200 alone proves only that
    *some* request succeeded, and the gateway may have rewritten it on the way (see
    ``_REWRITTEN``). A transient failure, a reply cut off at ``max_tokens`` before it could be
    judged, or a shape not reached within ``budget_seconds`` is *inconclusive*: it is reported
    but writes no flag, and it never refuses a model. ``report.capabilities`` therefore holds
    exactly the keys that were measured.
    """
    report = ProbeReport(model=model)
    started = time.monotonic()

    async def emit(event: dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            pending = on_progress(event)
            if inspect.isawaitable(pending):
                await pending
        except Exception:  # noqa: BLE001, S110 — progress is cosmetic; it must never change a verdict
            pass

    async def add(result: ShapeResult) -> None:
        report.results.append(result)
        await emit({"type": "result", **_result_dict(result)})

    cooled_down = False

    async def attempt(body: dict[str, Any], step: str) -> tuple[bool, Any, str, bool]:
        """Send one request of shape ``step`` (``shape`` or ``shape:detail``); (ok, response,
        provider reason, transient)."""
        nonlocal cooled_down
        shape = step.split(":", 1)[0]
        await emit({"type": "step", "shape": shape, "step": step, "label": STEP_LABELS.get(step, SHAPE_LABELS.get(shape, step))})
        if cooled_down:
            return False, None, _NOT_PROBED_COOLDOWN, True
        if time.monotonic() - started > budget_seconds:
            return False, None, f"probe budget of {budget_seconds:.0f}s exhausted before this shape", True
        try:
            response = await call(body)
            if body.get("stream"):
                response = assemble_stream(response if isinstance(response, str) else "")
            return True, response, "", False
        except ProbeCallError as e:
            if _is_cooldown(e.message):
                # The gateway took the deployment out of rotation after an earlier failure;
                # every further request would get this same answer, so none is sent.
                cooled_down = True
                return False, None, _NOT_PROBED_COOLDOWN, True
            return False, None, e.message, e.transient
        except Exception as e:  # noqa: BLE001 — a transport failure is inconclusive, not a crash
            return False, None, f"{type(e).__name__}: {e}", True

    async def graded(body: dict[str, Any], step: str, check: Callable[[Any], str]) -> tuple[bool, str, bool]:
        """Send one routable shape and judge the reply; (ok, reason, inconclusive)."""
        ok, response, err, transient = await attempt(body, step)
        if not ok:
            return False, err, transient
        failure = check(response)
        if failure and _truncated(response):
            return False, "reply cut off at max_tokens before the shape could be judged", True
        return not failure, failure, False

    # Unavoidable ----------------------------------------------------------------------
    for shape, body in (
        ("tools_auto", shape_tools_auto(model)),
        ("tool_round_trip", shape_tool_round_trip(model)),
        ("streaming_tools", shape_streaming_tools(model)),
    ):
        ok, _, err, transient = await attempt(body, shape)
        await add(ShapeResult(shape, ok, err, unavoidable=True, inconclusive=not ok and transient))

    # Forced tool choice: both forms must work for the flag to be True ------------------
    forced_ok, forced_err, forced_inc = await graded(shape_forced_tool_choice(model), "forced_tool_choice", _check_forced)
    await add(ShapeResult("forced_tool_choice", forced_ok, forced_err, inconclusive=forced_inc))
    named_ok, named_err, named_inc = await graded(shape_named_tool_choice(model), "named_tool_choice", _check_named)
    await add(ShapeResult("named_tool_choice", named_ok, named_err, inconclusive=named_inc))
    if forced_ok and named_ok:
        report.capabilities[FORCED_TOOL_CHOICE] = True
    elif (not forced_ok and not forced_inc) or (not named_ok and not named_inc):
        report.capabilities[FORCED_TOOL_CHOICE] = False  # a definite rejection of either form

    # response_format ------------------------------------------------------------------
    rf_ok, rf_err, rf_inc = await graded(shape_response_format(model), "response_format", _check_response_format)
    await add(ShapeResult("response_format", rf_ok, rf_err, inconclusive=rf_inc))
    if not rf_inc:
        report.capabilities[RESPONSE_FORMAT] = rf_ok

    # Thinking off: the first way that turns it off — an explicit switch, then the effort
    # alone — else it cannot be turned off at all ------------------------------------------
    # "Turns it off" means the reply shows no reasoning. That only means something if the same
    # question makes the model reason with thinking on, so a clean reply is checked against a
    # control turn (sent once, and only when needed); without that evidence the way is still
    # recorded, since it was accepted and nothing contradicts it, but noted as unverified.
    control: str | None = None  # "" = the control reasoned; otherwise why it could not verify

    async def verify() -> str:
        nonlocal control
        if control is None:
            ok, response, err, _ = await attempt(shape_thinking_control(model), "thinking_off:control")
            if not ok:
                control = f"unverified: the thinking-on control turn failed ({err})"
            elif not _reasoned(response):
                control = "unverified: the thinking-on control turn showed no reasoning either"
            else:
                control = ""
        return control

    def still_reasoned(response: Any) -> str:
        rt = _reasoning_tokens(response)
        return f"the model still reasoned ({rt} reasoning tokens)" if rt else "the model still reasoned"

    off_way: str | None = None  # the recorded value; None = inconclusive, no record
    off_ok, off_error, off_note = False, "", ""
    tried: list[str] = []
    for switch in (THINKING_OFF_DISABLED, THINKING_OFF_BETWEEN_TOOLS, None):
        label = switch or "reasoning_effort: none alone"
        ok, response, err, transient = await attempt(shape_thinking_off(model, switch), f"thinking_off:{switch or 'effort_only'}")
        if not ok and transient:
            off_error = err  # cannot tell which way works; leave no record
            break
        if not ok:
            tried.append(f"{label}: {err}")
            if switch is None:
                # Every way of asking for off is refused, the effort alone included: recorded
                # as its own value so the gateway stops sending what was just refused.
                off_way = THINKING_OFF_UNSUPPORTED
                off_error = "no thinking-off request is accepted — " + "; ".join(tried)
            continue
        if _reasoned(response):
            if switch is None:
                # Nothing turns it off: the floor is measured next (thinking_floor).
                tried.append(f"{label}: {still_reasoned(response)}")
                off_way = THINKING_OFF_ALWAYS_ON
                off_error = "thinking cannot be turned off — " + "; ".join(tried)
            else:
                tried.append(f"{label}: " + _REWRITTEN.format(what=still_reasoned(response)))
            continue
        rt = _reasoning_tokens(response)
        off_way, off_ok = (switch or THINKING_OFF_NONE), True
        off_note = "; ".join(n for n in (f"{rt} reasoning tokens" if rt is not None else "", await verify()) if n)
        break
    if off_way is None:
        await add(ShapeResult("thinking_off", False, off_error, inconclusive=True))
    else:
        await add(ShapeResult("thinking_off", off_ok, off_error, note=off_note))
        report.capabilities[THINKING_OFF] = off_way

    # Thinking floor: what a thinking-off request becomes where nothing turns thinking off -----
    # The lowest effort the deployment accepts, measured rather than read off the model's name:
    # `none` alone is the floor on some providers and the provider DEFAULT on others (Claude,
    # where LiteLLM sends no thinking parameter and no effort — nannos#330), and an effort the
    # gateway's own model map does not translate for this deployment is refused here, not on
    # the first real turn.
    if off_way is None:
        # Only an always-on deployment has a floor, and that was not established.
        await add(ShapeResult("thinking_floor", False, off_error, inconclusive=True))
    elif off_way != THINKING_OFF_ALWAYS_ON:
        await add(ShapeResult("thinking_floor", True, note="not needed: thinking can be turned off"))
    else:
        refused: list[str] = []
        floor_inconclusive = ""
        # The deployment's own declared levels when it has any — the ones the console's picker
        # offers, so the floor is always a level the admin also sees — else the fixed pair.
        candidates = tuple(floor_candidates or ())[:FLOOR_CANDIDATE_COUNT] or THINKING_FLOOR_CANDIDATES
        for effort in candidates:
            ok, _, err, transient = await attempt(shape_thinking_floor(model, effort), f"thinking_floor:{effort}")
            if ok:
                report.capabilities[THINKING_FLOOR] = effort
                await add(ShapeResult("thinking_floor", True, note=f"thinking-off requests are sent as reasoning_effort: {effort}"))
                break
            if transient:
                floor_inconclusive = err
                break
            refused.append(f"{effort}: {err}")
        else:
            await add(
                ShapeResult(
                    "thinking_floor",
                    False,
                    "no thinking level is accepted, so thinking-off requests keep the model's default effort — "
                    + "; ".join(refused),
                )
            )
        if floor_inconclusive:
            await add(ShapeResult("thinking_floor", False, floor_inconclusive, inconclusive=True))

    # Thinking replay: only for models declared to think ---------------------------------
    if supports_reasoning:
        on_ok, response, on_err, on_t = await attempt(shape_thinking_on(model), "thinking_replay:turn")
        assistant = _message(response) if on_ok else {}
        if on_ok and assistant.get("thinking_blocks"):
            ok, _, err, transient = await attempt(shape_thinking_replay(model, assistant), "thinking_replay:replay")
            await add(ShapeResult("thinking_replay", ok, err, inconclusive=not ok and transient))
            if ok or not transient:
                report.capabilities[THINKING_REPLAY] = ok
        elif on_ok:
            await add(ShapeResult("thinking_replay", True, note="no thinking block returned; nothing to replay"))
            report.capabilities[THINKING_REPLAY] = None
        else:
            await add(ShapeResult("thinking_replay", False, f"thinking turn failed: {on_err}", inconclusive=on_t))
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


_ABSENT = object()


def apply_thinking_off(kwargs: dict[str, Any], caps: dict[str, Any]) -> bool:
    """Rewrite a thinking-off request (``reasoning_effort: none``) into the shape the serving
    deployment's record says it takes. True when ``kwargs`` changed.

    Clients ask for thinking off with the effort alone; the switch is the deployment's, never
    the caller's, so any ``thinking`` a caller sent is dropped first (Gemini 3 400s on the pair).
    Then, by the record:

    * ``disabled`` / ``between_tools`` — that explicit ``thinking`` value is added;
    * ``none`` — the effort goes alone;
    * ``always_on`` — the effort becomes the measured THINKING_FLOOR (alone when none was);
    * ``unsupported`` — the effort is removed too: a plain request, the provider's default;
    * unprobed — nothing is guessed from the model's name: the effort goes alone.

    Top-level keys only: each attempt gets its own shallow copy from the router, so the change
    never leaks into a fallback attempt on another deployment — which reads its own record."""
    if kwargs.get("reasoning_effort") != "none":
        return False
    before = (kwargs.get("reasoning_effort", _ABSENT), kwargs.get("thinking", _ABSENT))
    kwargs.pop("thinking", None)
    way = caps.get(THINKING_OFF)
    if way in (THINKING_OFF_DISABLED, THINKING_OFF_BETWEEN_TOOLS):
        kwargs["thinking"] = {"type": way}
    elif way == THINKING_OFF_ALWAYS_ON and caps.get(THINKING_FLOOR):
        kwargs["reasoning_effort"] = caps[THINKING_FLOOR]
    elif way == THINKING_OFF_UNSUPPORTED:
        del kwargs["reasoning_effort"]
    return (kwargs.get("reasoning_effort", _ABSENT), kwargs.get("thinking", _ABSENT)) != before


def thinking_off_sendable(caps: dict[str, Any]) -> bool:
    """Whether a thinking-off request (``reasoning_effort: none``) may be sent to this deployment
    at all: the probe recorded a way it goes off (or that nothing does, where the gateway sends
    the measured floor instead). False when unprobed — no opinion, so a caller sends no effort and the
    provider default applies, as before any record existed — and False when every off request
    was refused (``unsupported``)."""
    return caps.get(THINKING_OFF) in (
        THINKING_OFF_DISABLED,
        THINKING_OFF_BETWEEN_TOOLS,
        THINKING_OFF_NONE,
        THINKING_OFF_ALWAYS_ON,
    )


def thinking_always_on(caps: dict[str, Any]) -> bool:
    """The probe saw that nothing turns this deployment's thinking off: the console offers no
    thinking-off for it, and a thinking-off request gets its lowest level instead. False when
    unprobed (no opinion)."""
    return caps.get(THINKING_OFF) == THINKING_OFF_ALWAYS_ON
