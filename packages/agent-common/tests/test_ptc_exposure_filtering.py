"""PTC ``eval`` namespace filtering: keep it minimal on both the normal and resume paths.

Two related guarantees, both undermined before these fixes:

1. **Normal model call** — the ``eval`` ``tools.*`` namespace must reflect exactly
   what the agent carries on ``request.tools`` (for the GP agent, the
   ToolsetSelector-filtered subset). It must NOT silently union the full build-time
   baseline (``broaden_baseline_tools``), which would re-expose every tool the
   selector dropped.

2. **Interrupt resume** — the eval tool node replays without a model call, so
   ``request.tools`` is empty. The namespace is rebuilt from ``broaden_baseline_tools``
   filtered to the exposure set checkpointed by ``aafter_model`` on the original call,
   so the resume REPL mirrors the original (filtered) namespace rather than the full
   registry. Absent a checkpointed set, it safely falls back to the full baseline.

Plus the ``execute`` execution-gate: ``FilesystemMiddleware`` binds a dead ``execute``
even on non-sandbox backends; it must be kept out of the PTC namespace AND stripped
from the model's bound tools when the backend cannot run commands.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

import agent_common.core.graph_utils as gu


@pytest.fixture(autouse=True)
def _ptc_enabled(monkeypatch):
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")


def _tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=name)


def _mw(*, supports_execution: bool, static=None, baseline=None):
    return gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=static if static is not None else [_tool("read_file")],
        broaden_baseline_tools=baseline if baseline is not None else [],
        ptc_enabled=True,
        broaden_exposure=True,
        backend_supports_execution=supports_execution,
    )


def _req(**kwargs):
    return SimpleNamespace(**kwargs)


def test_normal_path_does_not_union_full_baseline():
    """On a model call, request.tools is authoritative; dropped baseline tools stay dropped."""
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    names = {t.name for t in mw._collect_ptc_tools(_req(tools=[_tool("mcp_a")], state={}))}
    assert "mcp_a" in names
    assert "mcp_b" not in names, f"baseline tool re-exposed despite filtering; got {names}"
    assert "read_file" in names  # static fs baseline always present


def test_execute_not_exposed_on_non_sandbox_request_harvest():
    """A dead ``execute`` arriving via request.tools must not reach the PTC namespace."""
    mw = _mw(supports_execution=False)
    names = {t.name for t in mw._collect_ptc_tools(_req(tools=[_tool("execute"), _tool("mcp_a")], state={}))}
    assert "execute" not in names, f"dead execute leaked into PTC namespace; got {names}"
    assert "mcp_a" in names


def test_execute_exposed_once_on_sandbox():
    mw = _mw(supports_execution=True, static=[_tool("execute")])
    collected = mw._collect_ptc_tools(_req(tools=[_tool("execute")], state={}))
    names = [t.name for t in collected]
    assert names.count("execute") == 1, f"execute should be exposed exactly once; got {names}"


def test_resume_filters_baseline_by_checkpointed_set():
    """Resume (no request.tools) rebuilds from baseline filtered to the checkpointed set."""
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    req = _req(state={gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY: ["mcp_a"]})
    names = {t.name for t in mw._collect_ptc_tools(req)}
    assert "mcp_a" in names
    assert "mcp_b" not in names, f"resume re-exposed a tool not in the checkpointed set; got {names}"
    assert "read_file" in names


def test_resume_without_checkpoint_falls_back_to_full_baseline():
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    names = {t.name for t in mw._collect_ptc_tools(_req(state={}))}
    assert {"mcp_a", "mcp_b", "read_file"} <= names


def test_prompt_relays_exposure_and_strips_dead_execute():
    mw = _mw(supports_execution=False)
    mw._prepare_for_call = lambda req: "PROMPT"  # avoid heavy REPL setup
    _prompt, hidden = mw._ptc_prompt_and_hidden(_req(tools=[_tool("execute"), _tool("mcp_a")], state={}))
    # Dead execute is stripped from the model's bound tools even though not PTC-exposed.
    assert "execute" in hidden
    assert {"mcp_a", "read_file"} <= hidden
    # The relayed exposure set (for the checkpoint) excludes the dead execute.
    relayed = gu._ptc_exposed_names_var.get()
    assert relayed is not None
    assert "execute" not in relayed
    assert "mcp_a" in relayed


@pytest.mark.asyncio
async def test_aafter_model_checkpoints_relayed_exposure():
    mw = _mw(supports_execution=False)
    mw._prepare_for_call = lambda req: "PROMPT"
    mw._ptc_prompt_and_hidden(_req(tools=[_tool("mcp_a")], state={}))
    update = await mw.aafter_model({}, None)
    assert update == {gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY: gu._ptc_exposed_names_var.get()}
    assert "mcp_a" in update[gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY]


def test_state_key_matches_state_field():
    assert gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY in gu._PTCExposureState.__annotations__


class _OverridableReq:
    """Minimal model-call request supporting ``.override(tools=...)`` for _strip_hidden."""

    def __init__(self, tools, state=None):
        self.tools = tools
        self.state = state or {}

    def override(self, **kwargs):
        new = _OverridableReq(self.tools, self.state)
        for k, v in kwargs.items():
            setattr(new, k, v)
        return new


def test_orchestrator_curated_utility_exposed_and_hidden():
    """Option D end-to-end runtime guarantee, in the orchestrator's configuration.

    With ``broaden_exposure=False`` (orchestrator) and a curated utility folded into
    the static baseline (as ``build_code_interpreter_middlewares`` does with
    ``extra_static_ptc_tools``), on a model call the utility is exposed inside
    ``eval`` AND stripped from the model's bound tools, while the never-exposed
    native ``task``/``eval`` stay visible — and ``request.tools`` is NOT harvested."""
    mw = gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_tool("read_file"), _tool("get_current_time")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=False,  # orchestrator
        backend_supports_execution=False,
    )
    mw._prepare_for_call = lambda req: "PROMPT"  # avoid heavy REPL setup

    # The model's native surface this turn: the utility (still bound), task, eval.
    req = _OverridableReq(tools=[_tool("get_current_time"), _tool("task"), _tool("eval")])
    _prompt, hidden = mw._ptc_prompt_and_hidden(req)

    # Curated utility is exposed-in-eval → hidden from the model.
    assert "get_current_time" in hidden
    # Dispatch / eval primitives are never PTC-exposed → stay bound.
    assert "task" not in hidden
    assert "eval" not in hidden
    # broaden_exposure=False: request.tools are not harvested into the eval namespace.
    exposed = set(gu._ptc_exposed_names_var.get() or [])
    assert exposed == {"read_file", "get_current_time"}, f"only the static baseline should be exposed; got {exposed}"

    # _strip_hidden actually removes the utility from the bound list, keeps task/eval.
    kept = {t.name for t in mw._strip_hidden(req, hidden).tools}
    assert kept == {"task", "eval"}, f"utility must be stripped; task/eval kept; got {kept}"


class _RegistryReq:
    """Model-call request carrying dict schemas in ``tools`` (as DynamicToolDispatch
    injects) and the real BaseTools in ``runtime.context.tool_registry`` — plus the
    ``whitelisted_tool_names`` the orchestrator actually binds."""

    def __init__(self, tools, tool_registry, whitelist=None):
        self.tools = tools
        self.state = {}
        self._tool_registry = tool_registry
        self._whitelist = set(tool_registry) if whitelist is None else set(whitelist)
        self.runtime = SimpleNamespace(
            context=SimpleNamespace(tool_registry=tool_registry, whitelisted_tool_names=self._whitelist)
        )

    def override(self, **kwargs):
        new = _RegistryReq(self.tools, self._tool_registry, self._whitelist)
        for k, v in kwargs.items():
            setattr(new, k, v)
        return new


def test_context_registry_exposed_via_eval_and_dict_schemas_stripped():
    """Orchestrator 'eval + task': the per-user runtime ``tool_registry`` is exposed
    inside ``eval`` (sourced from the context, not ``request.tools``), and the
    natively-injected dict schemas for those tools are stripped from the model's
    bound list — while ``task``/``eval`` stay native. Dispatch logic is untouched."""
    mcp_tool = _tool("gdrive_copy_file")
    mw = gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_tool("read_file")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=False,  # orchestrator
        backend_supports_execution=False,
        expose_context_registry=True,  # the new flag
    )
    mw._prepare_for_call = lambda req: "PROMPT"  # avoid heavy REPL setup

    # request.tools carries provider-native dict schemas (as dispatch injects);
    # the executable BaseTool lives in the runtime context registry.
    req = _RegistryReq(
        tools=[
            {"function": {"name": "gdrive_copy_file"}},
            {"function": {"name": "task"}},
            {"function": {"name": "eval"}},
        ],
        tool_registry={"gdrive_copy_file": mcp_tool},
    )
    _prompt, hidden = mw._ptc_prompt_and_hidden(req)

    # Registry tool is exposed in eval (sourced from context) → hidden from bound list.
    assert "gdrive_copy_file" in hidden
    assert "task" not in hidden and "eval" not in hidden
    exposed = set(gu._ptc_exposed_names_var.get() or [])
    assert exposed == {"read_file", "gdrive_copy_file"}, f"got {exposed}"

    # The injected dict schema for the registry tool is stripped; task/eval dicts kept.
    kept = {mw._tool_name_of(t) for t in mw._strip_hidden(req, hidden).tools}
    assert kept == {"task", "eval"}, f"registry dict must be stripped; task/eval kept; got {kept}"


def test_context_registry_only_exposes_whitelisted_subset():
    """The full ``tool_registry`` is the whole per-user catalog; only the
    orchestrator's whitelisted subset (system tools + user-enabled) is exposed via
    eval — mirroring the bind-time filter — NOT the entire catalog."""
    mw = gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_tool("read_file")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=False,
        backend_supports_execution=False,
        expose_context_registry=True,
    )
    # Registry holds the whole catalog; whitelist is the ~bound subset.
    req = _RegistryReq(
        tools=[],
        tool_registry={
            "gdrive_copy_file": _tool("gdrive_copy_file"),  # user-enabled → whitelisted
            "scheduler_list_jobs": _tool("scheduler_list_jobs"),  # system → whitelisted
            "github_search_code": _tool("github_search_code"),  # NOT whitelisted
            "github_list_issues": _tool("github_list_issues"),  # NOT whitelisted
        },
        whitelist={"gdrive_copy_file", "scheduler_list_jobs"},
    )
    names = {t.name for t in mw._collect_ptc_tools(req)}
    assert names == {"read_file", "gdrive_copy_file", "scheduler_list_jobs"}, (
        f"only the whitelisted subset (+fs baseline) should be exposed; got {names}"
    )


def test_context_registry_not_harvested_when_flag_off():
    """Without the flag, the runtime registry is NOT pulled into eval (current default)."""
    mw = gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_tool("read_file")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=False,
        backend_supports_execution=False,
        expose_context_registry=False,
    )
    req = _RegistryReq(tools=[], tool_registry={"gdrive_copy_file": _tool("gdrive_copy_file")})
    names = {t.name for t in mw._collect_ptc_tools(req)}
    assert names == {"read_file"}, f"registry must not be exposed when flag off; got {names}"
