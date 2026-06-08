"""Unit tests for the PTC runtime risk-guard wrappers (ptc_guard)."""

from __future__ import annotations

import types
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent_common.middleware import ptc_guard
from agent_common.middleware.ptc_guard import (
    HiddenToolsFromModelMiddleware,
    _inject_for_inner,
    _resolve_server_slug,
    approval_required_payload,
    wrap_tool_for_ptc,
)


class _Args(BaseModel):
    path: str = Field(description="path")


def _make_inner(record: list[dict[str, Any]]) -> BaseTool:
    async def _inner(path: str) -> str:
        record.append({"path": path})
        return f"read:{path}"

    return StructuredTool.from_function(
        coroutine=_inner, name="read_file", description="read a file", args_schema=_Args
    )


def _make_runtime(
    *,
    bypass_rules: dict | None = None,
    tool_server_map: dict | None = None,
    risk_threshold: float | None = None,
) -> types.SimpleNamespace:
    context = types.SimpleNamespace(
        tool_bypass_rules=bypass_rules,
        tool_server_map=tool_server_map,
        tool_risk_cache=None,
        risk_threshold=risk_threshold,
    )
    return types.SimpleNamespace(context=context, state={}, store=None)


def _scorer(score: float, *, recorder: dict | None = None):
    async def _fn(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
        if recorder is not None:
            recorder.update(
                {
                    "tool_name": tool_name,
                    "args": dict(args),
                    "server_slug": server_slug,
                    "tool": tool,
                }
            )
        return score, None

    return _fn


# ---------------------------------------------------------------------------
# approval_required_payload
# ---------------------------------------------------------------------------


def test_approval_payload_carries_tool_name_and_guidance():
    payload = approval_required_payload("write_file")
    assert payload["error"] == "human_approval_required"
    assert payload["tool"] == "write_file"
    assert "write_file" in payload["message"]
    assert "approval" in payload["message"].lower()


# ---------------------------------------------------------------------------
# wrap_tool_for_ptc: schema preservation
# ---------------------------------------------------------------------------


def test_wrapper_preserves_name_description_and_hides_runtime():
    inner = _make_inner([])
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.0))
    assert wrapped.name == "read_file"
    assert wrapped.description == "read a file"
    # The injected ``runtime`` must not be model-facing.
    assert set(wrapped.args.keys()) == {"path"}


def test_wrapper_marks_runtime_as_injected_arg():
    """Regression: ``from __future__ import annotations`` turns ``_guarded``'s
    ``runtime`` annotation into a string, which would make
    ``StructuredTool._injected_args_keys`` miss it. If that happens,
    ``BaseTool._parse_input`` strips the PTC-injected ``runtime`` and the inner
    tool crashes with a missing ``runtime`` argument."""
    inner = _make_inner([])
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.0))
    assert "runtime" in wrapped._injected_args_keys


async def test_arun_delivers_injected_runtime_to_guard(monkeypatch):
    """Regression: the real PTC bridge calls ``wrapped.arun({..., "runtime": rt})``.
    The wrapper reuses the inner tool's LLM-facing ``args_schema`` (no ``runtime``
    field), so ``runtime`` only survives ``BaseTool._parse_input`` if the wrapper
    recognises it as an injected arg. If stripped, ``_guarded`` receives
    ``runtime=None`` and the inner tool crashes with a missing ``runtime``.

    Asserts ``runtime`` reaches ``_guarded`` (and is forwarded to the inner via
    ``_inject_for_inner``) when invoked through the real ``arun`` path."""
    captured: dict[str, Any] = {}
    real_inject = ptc_guard._inject_for_inner

    def _spy(inner, kwargs, runtime):  # noqa: ANN001, ANN202
        captured["runtime"] = runtime
        return real_inject(inner, kwargs, runtime)

    monkeypatch.setattr(ptc_guard, "_inject_for_inner", _spy)

    record: list[dict] = []
    inner = _make_inner(record)  # inner takes only ``path``; no runtime needed
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=None)
    rt = _make_runtime()

    out = await wrapped.arun({"path": "/tmp/x", "runtime": rt}, tool_call_id="c1")

    assert getattr(out, "content", out) == "read:/tmp/x"
    assert record == [{"path": "/tmp/x"}]
    # The injected runtime survived ``_parse_input`` and reached the guard.
    assert captured["runtime"] is rt


# ---------------------------------------------------------------------------
# wrap_tool_for_ptc: guard decision
# ---------------------------------------------------------------------------


async def test_high_score_returns_payload_and_does_not_execute():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.9), default_risk_threshold=0.8)
    rt = _make_runtime()
    out = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
    assert out["error"] == "human_approval_required"
    assert out["tool"] == "read_file"
    assert record == []  # inner never executed


async def test_low_score_executes_inner():
    record: list[dict] = []
    inner = _make_inner(record)
    seen: dict = {}
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.2, recorder=seen), default_risk_threshold=0.8)
    rt = _make_runtime()
    out = await wrapped.coroutine(runtime=rt, path="/tmp/x")
    assert out == "read:/tmp/x"
    assert record == [{"path": "/tmp/x"}]
    assert seen["tool_name"] == "read_file"
    assert seen["args"] == {"path": "/tmp/x"}
    assert seen["tool"] is inner
    assert seen["server_slug"] == "_self"


async def test_no_scorer_executes_without_guard():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=None)
    rt = _make_runtime()
    out = await wrapped.coroutine(runtime=rt, path="/tmp/y")
    assert out == "read:/tmp/y"
    assert record == [{"path": "/tmp/y"}]


async def test_scorer_exception_skips_guard_and_executes():
    record: list[dict] = []
    inner = _make_inner(record)

    async def _boom(*a, **k):
        raise RuntimeError("scorer down")

    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_boom)
    rt = _make_runtime()
    out = await wrapped.coroutine(runtime=rt, path="/tmp/z")
    assert out == "read:/tmp/z"
    assert record == [{"path": "/tmp/z"}]


# ---------------------------------------------------------------------------
# wrap_tool_for_ptc: per-user bypass rules
# ---------------------------------------------------------------------------


async def test_bypass_all_skips_scoring_and_executes():
    record: list[dict] = []
    inner = _make_inner(record)
    seen: dict = {}
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.99, recorder=seen))
    rt = _make_runtime(bypass_rules={"read_file::_self": {"bypass_all": True}})
    out = await wrapped.coroutine(runtime=rt, path="/etc/shadow")
    assert out == "read:/etc/shadow"
    assert record == [{"path": "/etc/shadow"}]
    assert seen == {}  # scorer never consulted


async def test_bypass_pattern_match_skips_scoring():
    record: list[dict] = []
    inner = _make_inner(record)
    seen: dict = {}
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.99, recorder=seen))
    rt = _make_runtime(bypass_rules={"read_file::_self": {"bypass_patterns": {"path": ["/tmp/*"]}}})
    out = await wrapped.coroutine(runtime=rt, path="/tmp/ok")
    assert out == "read:/tmp/ok"
    assert seen == {}


async def test_bypass_pattern_non_match_still_guards():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.99), default_risk_threshold=0.8)
    rt = _make_runtime(bypass_rules={"read_file::_self": {"bypass_patterns": {"path": ["/tmp/*"]}}})
    out = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
    assert out["error"] == "human_approval_required"
    assert record == []


# ---------------------------------------------------------------------------
# wrap_tool_for_ptc: threshold override + server slug
# ---------------------------------------------------------------------------


async def test_per_request_threshold_override_allows_call():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.9), default_risk_threshold=0.8)
    rt = _make_runtime(risk_threshold=0.95)  # raise bar above the 0.9 score
    out = await wrapped.coroutine(runtime=rt, path="/x")
    assert out == "read:/x"
    assert record == [{"path": "/x"}]


async def test_server_slug_resolved_from_context_map():
    seen: dict = {}
    inner = _make_inner([])
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.0, recorder=seen))
    rt = _make_runtime(tool_server_map={"read_file": "console"})
    await wrapped.coroutine(runtime=rt, path="/x")
    assert seen["server_slug"] == "console"


def test_resolve_server_slug_precedence():
    ctx = types.SimpleNamespace(tool_server_map={"t": "ctx_server"})
    assert _resolve_server_slug("t", ctx, {"t": "static_server"}) == "ctx_server"
    assert _resolve_server_slug("t", None, {"t": "static_server"}) == "static_server"
    assert _resolve_server_slug("t", None, None) == "_self"


# ---------------------------------------------------------------------------
# _inject_for_inner
# ---------------------------------------------------------------------------


def test_inject_for_inner_without_injected_args_is_passthrough():
    inner = _make_inner([])
    rt = _make_runtime()
    enriched = _inject_for_inner(inner, {"path": "/a"}, rt)  # type: ignore[arg-type]
    assert enriched == {"path": "/a"}


def test_inject_for_inner_forwards_runtime_when_declared():
    async def _needs_rt(runtime: ToolRuntime, path: str) -> str:
        return path

    inner = StructuredTool.from_function(coroutine=_needs_rt, name="rt_tool", description="d", args_schema=_Args)
    rt = _make_runtime()
    enriched = _inject_for_inner(inner, {"path": "/a"}, rt)  # type: ignore[arg-type]
    assert enriched["path"] == "/a"
    assert enriched["runtime"] is rt


# ---------------------------------------------------------------------------
# HiddenToolsFromModelMiddleware
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, tools: list[BaseTool]) -> None:
        self.tools = tools
        self.override_calls: list[dict] = []

    def override(self, **overrides):
        self.override_calls.append(overrides)
        new = _FakeRequest(overrides.get("tools", self.tools))
        return new


def test_hidden_middleware_strips_hidden_tools():
    a = _make_inner([])  # name read_file
    b = StructuredTool.from_function(coroutine=a.coroutine, name="write_file", description="d", args_schema=_Args)
    req = _FakeRequest([a, b])
    mw = HiddenToolsFromModelMiddleware({"read_file"})
    filtered = mw._filter(req)
    names = {t.name for t in filtered.tools}
    assert names == {"write_file"}


def test_hidden_middleware_noop_when_nothing_hidden():
    a = _make_inner([])
    req = _FakeRequest([a])
    mw = HiddenToolsFromModelMiddleware({"does_not_exist"})
    filtered = mw._filter(req)
    assert filtered is req  # unchanged, no override
    assert req.override_calls == []


# ---------------------------------------------------------------------------
# Per-turn collector + decision-aware guard
# ---------------------------------------------------------------------------


_TID = ptc_guard._PTC_DEFAULT_THREAD_ID


def _runtime_with_thread(thread_id: str) -> types.SimpleNamespace:
    rt = _make_runtime()
    rt.config = {"configurable": {"thread_id": thread_id}}
    return rt


def test_default_thread_id_constant_is_used_without_config():
    assert ptc_guard.resolve_ptc_thread_id(_make_runtime()) == _TID


def test_call_key_is_stable_and_arg_sensitive():
    k1 = ptc_guard._call_key("read_file", {"path": "/a"})
    k2 = ptc_guard._call_key("read_file", {"path": "/a"})
    k3 = ptc_guard._call_key("read_file", {"path": "/b"})
    assert k1 == k2
    assert k1 != k3


async def test_high_score_records_pending_when_turn_active():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.9), default_risk_threshold=0.8)
    rt = _runtime_with_thread("t1")
    ptc_guard.begin_ptc_turn("t1")
    try:
        out = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
        assert out["error"] == "human_approval_required"
        assert record == []  # inner not executed
        pending = ptc_guard.take_ptc_pending("t1")
        assert len(pending) == 1
        assert pending[0].tool_name == "read_file"
        assert pending[0].args == {"path": "/etc/passwd"}
        assert "edit" not in pending[0].allowed_actions
    finally:
        ptc_guard.end_ptc_turn("t1")


async def test_recorded_reject_decision_returns_rejection_payload():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.9), default_risk_threshold=0.8)
    rt = _runtime_with_thread("t2")
    turn = ptc_guard.begin_ptc_turn("t2")
    try:
        call_key = ptc_guard._call_key("read_file", {"path": "/etc/passwd"})
        turn.decisions[call_key] = "reject"
        out = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
        assert out["error"] == "human_rejected"
        assert record == []  # inner never executed on reject
    finally:
        ptc_guard.end_ptc_turn("t2")


async def test_recorded_approve_decision_executes_and_caches():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.99), default_risk_threshold=0.8)
    rt = _runtime_with_thread("t3")
    turn = ptc_guard.begin_ptc_turn("t3")
    try:
        call_key = ptc_guard._call_key("read_file", {"path": "/etc/passwd"})
        turn.decisions[call_key] = "approve"
        out = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
        assert out == "read:/etc/passwd"
        assert record == [{"path": "/etc/passwd"}]
        assert turn.results[call_key] == "read:/etc/passwd"

        # A second call within the same process execution hits the result cache.
        out2 = await wrapped.coroutine(runtime=rt, path="/etc/passwd")
        assert out2 == "read:/etc/passwd"
        assert record == [{"path": "/etc/passwd"}]  # inner not re-executed
    finally:
        ptc_guard.end_ptc_turn("t3")


async def test_low_score_caches_result_when_turn_active():
    record: list[dict] = []
    inner = _make_inner(record)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.1), default_risk_threshold=0.8)
    rt = _runtime_with_thread("t4")
    turn = ptc_guard.begin_ptc_turn("t4")
    try:
        out = await wrapped.coroutine(runtime=rt, path="/data/a")
        assert out == "read:/data/a"
        call_key = ptc_guard._call_key("read_file", {"path": "/data/a"})
        assert turn.results[call_key] == "read:/data/a"
    finally:
        ptc_guard.end_ptc_turn("t4")
