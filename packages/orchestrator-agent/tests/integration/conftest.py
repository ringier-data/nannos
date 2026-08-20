"""Fixtures for integration tests that make real LLM calls.

All LLM traffic routes through the LiteLLM gateway — there is no per-provider
fallback (``agent_common.core.model_factory`` raises when ``LLM_GATEWAY_URL`` is
unset). So these tests need a reachable gateway and nothing else:

    LLM_GATEWAY_URL=http://localhost:<port>
    LLM_GATEWAY_API_KEY=sk-...

Provider credentials (Bedrock / Azure / Vertex) belong to the *gateway*, not to
the test process. Do not put them in a test env file.

Setup:
  1. Start a local gateway: ``./scripts/start-local.sh`` from the repo root
     (defaults: http://localhost:4000, key ``sk-nannos-local``). Or port-forward
     the deployed litellm-proxy.
  2. Put the two variables in packages/orchestrator-agent/.env or in
     tests/integration/.env.integration — start-local.sh exports them only for
     the services it launches, so a separate pytest process does not inherit them.
  3. cd packages/orchestrator-agent && uv run pytest tests/integration/ -m integration

Overrides go in tests/integration/.env.integration, which takes priority over
the orchestrator .env. Both are gitignored — verify with ``git check-ignore``
before putting a real key in either; this is a public repo.

Selection: these tests are collected on every run but deselected by the
``-m "not integration"`` default in pyproject addopts, so breakage here shows up
immediately while normal runs stay fast. They previously used ``--ignore``,
which hid the directory entirely — an a2a-sdk migration broke an import in
``test_a2a_streaming.py`` and nothing noticed for months.

Because a marker expression is last-wins, that default alone is not a spend
guard: ``-m slow`` would replace it and select exactly this directory. So
``pytest_collection_modifyitems`` below *also* requires the tier to be asked for
explicitly — ``-m integration`` or ``RUN_INTEGRATION_TESTS=1`` — and skips when
no gateway is reachable, rather than failing mid-call.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Pure stdlib, so it is safe to import here rather than in the heavy block below —
# and it must be, because model discovery runs at import time and needs it.
from tests.support.gateway_env import (
    GATEWAY_KEYS,
    gateway_env,
    layered_env,
    restored_environment,
)

# ---------------------------------------------------------------------------
# Credential restoration: undo pytest-env fake values for integration tests
# ---------------------------------------------------------------------------
# pytest-env (configured in pyproject.toml) sets AWS_ACCESS_KEY_ID=testing etc.
# at startup, clobbering real credentials from the shell. We need to undo this
# so integration tests can use real provider credentials.
#
# Both env files are layered per key, with .env.integration winning — see
# tests/support/gateway_env for why that has to be identical here and at
# collection time. Then pytest-env's placeholders are stripped so the provider
# SDKs fall back to the default chains (~/.aws/credentials, az login, gcloud).

_ENV_INTEGRATION_FILE = Path(__file__).parent / ".env.integration"
_ORCHESTRATOR_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


def _restore_real_credentials() -> None:
    """Swap pytest-env's placeholders for the real environment, in place.

    A thin shell: the layering itself — which file wins for which key — lives in
    ``tests/support/gateway_env`` so that discovery at import time and this,
    at runtime, cannot drift apart. They did, twice; see that module's docstring.
    """
    desired = restored_environment(os.environ, _ENV_INTEGRATION_FILE, _ORCHESTRATOR_ENV_FILE)
    stripped = set(os.environ) - set(desired)

    os.environ.update(desired)
    for key in stripped:
        os.environ.pop(key, None)

    logging.getLogger(__name__).info(
        "Integration environment applied: %d var(s) from %s / %s, %d fake credential(s) removed",
        len(layered_env(_ENV_INTEGRATION_FILE, _ORCHESTRATOR_ENV_FILE)),
        _ENV_INTEGRATION_FILE.name,
        _ORCHESTRATOR_ENV_FILE.name,
        len(stripped),
    )


# NOTE: _restore_real_credentials() is deliberately NOT called at import time.
#
# This directory is collected on every run (it is deselected by the `-m "not
# integration"` default, not hidden), so anything this module does at import
# reaches the whole session. Deleting pytest-env's fake AWS credentials and
# forcing LANGSMITH_TRACING=true used to happen here, which broke unrelated unit
# tests and spammed LangSmith 401s the moment the directory became visible.
# Those mutations now live in the `integration_environment` autouse fixture
# below, which only applies to tests under this directory.


def _gateway_env_from_files() -> dict[str, str]:
    """Gateway coordinates, layered per key across both env files.

    Same loader as ``_restore_real_credentials`` on purpose. Reading them
    per file here and per key there is what let a valid split configuration
    report "No Model Gateway".
    """
    return gateway_env(_ENV_INTEGRATION_FILE, _ORCHESTRATOR_ENV_FILE)


import pytest
from agent_common.core.model_factory import (
    _gateway_models,  # no public accessor for model_info; see _is_chat_model
    assert_gateway_configured,
    get_available_models,
    get_model_provider,
)
from agent_common.models.base import ModelType, ThinkingLevel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr

from app.core.agent import OrchestratorDeepAgent
from app.models.config import AgentSettings, UserConfig
from tests.support.eval_report import EvalSession, min_pass_ratio
from tests.support.graph_harness import patched_factory
from tests.support.marker_gate import (
    ENV_OPT_IN,
    integration_possibly_requested,
    integration_requested,
)
from tests.support.mock_subagents import MockSubAgent, mock_subagents
from tests.support.usage import UsageRecorder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model discovery — the gateway is the single source of truth
# ---------------------------------------------------------------------------
# There is no per-provider credential detection any more. Which models exist is
# decided by the gateway registry (/v1/model/info), and whether they can be
# called is decided by the credentials the *gateway* holds. Probing for AWS/Azure/
# GCP credentials in this process would answer a question nobody asks: the test
# process never talks to a provider directly.
#
# Everything below is resolved once at import time, because parametrize needs the
# model list at collection. When the gateway is unreachable the list is empty and
# the parametrized tests report an empty parameter set rather than pretending a
# credential is missing.


def _is_chat_model(model_type: ModelType) -> bool:
    """Whether the gateway serves this alias as a chat model.

    ``get_available_models()`` returns the whole registry — embedding models
    included — so parametrizing chat tests over it would send prompts to e.g.
    ``titan-embed-text-v2``. The ``mode`` that distinguishes them lives in the
    gateway's model_info, which no public helper exposes, hence the private
    ``_gateway_models``. Unknown mode counts as chat so a registration that
    omits the field is exercised rather than silently skipped.
    """
    info = _gateway_models().get(model_type) or {}
    return info.get("mode", "chat") == "chat"


def _discover_models() -> list[ModelType]:
    """Chat model aliases the gateway currently serves; empty when unreachable.

    Runs at import because parametrize needs the list at collection time, so the
    gateway variables are applied and then rolled back — leaving them set would
    change the environment for every other test in the session.

    ``get_available_models`` swallows fetch failures and returns an empty registry,
    so a down gateway costs one ~2s timeout at collection, not an error.

    Skipped outright when nothing asked for this tier. Otherwise every plain unit
    run pays for a live HTTP fetch, which the marker-based selection made
    unavoidable: the directory is collected rather than ignored, so this module
    is imported whether or not its tests will run. Measured at +4.8s when the
    gateway hostname does not resolve — DNS is not bounded by the socket timeout.
    Returning an empty list is already the well-trodden path, identical to a
    gateway being down: parametrized tests report an empty parameter set and the
    collection hook skips the directory as not requested.
    """
    if not integration_possibly_requested():
        logger.debug("Integration tier not requested — skipping gateway discovery.")
        return []

    saved = {k: os.environ.get(k) for k in GATEWAY_KEYS}
    os.environ.update(_gateway_env_from_files())
    try:
        assert_gateway_configured()
    except Exception as exc:  # RuntimeError when LLM_GATEWAY_URL is unset
        logger.warning("Model Gateway not configured, integration tests will skip: %s", exc)
        return []
    else:
        try:
            return [m for m in get_available_models() if _is_chat_model(m)]
        except Exception as exc:
            logger.warning("Could not reach the Model Gateway at %s: %s", os.getenv("LLM_GATEWAY_URL"), exc)
            return []
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


ALL_MODELS: list[ModelType] = _discover_models()

GATEWAY_AVAILABLE: bool = bool(ALL_MODELS)

# Thinking levels to exercise per model. Only the portable tier is used: low,
# medium and high are accepted by every reasoning provider, while minimal/xhigh
# are gated on per-model gateway capability flags that this process cannot read
# (they live in model_info, which console-backend serves). The gateway drops the
# parameter entirely for non-reasoning models, so one level per model is both
# safe and enough — and it keeps a real-LLM parametrization from multiplying out.
THINKING_MODELS: dict[ModelType, list[ThinkingLevel]] = {model: [ThinkingLevel.low] for model in ALL_MODELS}


# ---------------------------------------------------------------------------
# Module-level marker: all tests in this directory are integration tests
# ---------------------------------------------------------------------------

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Environment, scoped to this directory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def integration_environment():
    """Swap the fake test environment for the real one, for these tests only.

    pytest-env sets AWS_ACCESS_KEY_ID=testing and LANGSMITH_TRACING=false for the
    whole session. Integration tests need the opposite. Doing that at import time
    leaked into every other test once this directory became collectable, so it is
    a fixture — autouse and session-scoped, but resolved only when a test under
    this directory actually runs — and it puts the environment back afterwards.
    """
    before = dict(os.environ)

    _restore_real_credentials()

    # Only trace when a real key is present. pytest-env seeds LANGSMITH_API_KEY
    # with "test-key"; forcing tracing on with that makes the langsmith plugin
    # 401 during setup, which surfaces as every test ERRORing for a reason that
    # has nothing to do with the code under test.
    langsmith_key = os.environ.get("LANGSMITH_API_KEY", "")
    if langsmith_key and langsmith_key != "test-key":
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ.setdefault("LANGSMITH_PROJECT", "integration-tests-orchestrator")
    else:
        os.environ["LANGSMITH_TRACING"] = "false"
        # LANGSMITH_TRACING alone is not enough: the `@pytest.mark.langsmith`
        # marker drives the langsmith pytest plugin directly, and it reaches out
        # to create a test suite during setup regardless of that flag. Without a
        # valid key that is a 401 and every marked test FAILS for a reason that
        # has nothing to do with the code. LANGSMITH_TEST_TRACKING is the
        # plugin's own off-switch.
        os.environ["LANGSMITH_TEST_TRACKING"] = "false"
        logger.info("No real LANGSMITH_API_KEY — LangSmith tracing and test tracking disabled.")

    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def pytest_collection_modifyitems(config, items):
    """Skip this directory unless it was asked for *and* a gateway is reachable.

    Two independent guards, both applied at collection rather than in a fixture
    so the skip lands before the langsmith plugin's per-test setup, which
    otherwise runs (and can fail) first. Only items under this directory are
    touched — the hook is handed the whole session's items regardless of which
    conftest defines it.

    1. **Opt-in.** A user-supplied ``-m`` replaces the ``not integration``
       default outright, and every module here also carries ``slow``, so a
       casual ``pytest -m slow`` would otherwise make real, billable LLM calls.
       See ``tests/support/marker_gate`` for how intent is read.
    2. **Reachable gateway.** Skip with a reason that says so, rather than
       failing mid-call.
    """
    here = Path(__file__).parent
    markexpr = getattr(config.option, "markexpr", "")

    not_requested = pytest.mark.skip(
        reason=(
            "Integration tier not requested. Run it with `-m integration`, or set "
            f"{ENV_OPT_IN}=1 when selecting these tests by path or keyword."
        )
    )
    no_gateway = pytest.mark.skip(
        reason=(
            "No Model Gateway. Start one with ./scripts/start-local.sh and set "
            "LLM_GATEWAY_URL / LLM_GATEWAY_API_KEY in packages/orchestrator-agent/.env"
        )
    )

    for item in items:
        try:
            item_path = Path(str(item.fspath))
        except Exception:
            continue
        if here not in item_path.parents:
            continue
        if not integration_requested(markexpr, [m.name for m in item.iter_markers()]):
            item.add_marker(not_requested)
        elif not GATEWAY_AVAILABLE:
            item.add_marker(no_gateway)


# ---------------------------------------------------------------------------
# LangSmith experiment metadata (used by langsmith pytest plugin)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def langsmith_experiment_metadata():
    """Attach metadata to the LangSmith experiment for this test run."""
    return {
        "environment": os.environ.get("ENV", "local"),
        "models": ", ".join(available_models() or ["none-available"]),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def memory_checkpointer():
    """In-memory checkpointer (replaces DynamoDB for tests)."""
    return MemorySaver()


@pytest.fixture(scope="session")
def memory_store():
    """In-memory store (replaces PostgreSQL + pgvector for tests)."""
    return InMemoryStore()


@pytest.fixture()
def context_id():
    """Unique context ID per test (conversation isolation)."""
    return f"test-ctx-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def test_user_config() -> UserConfig:
    """Minimal UserConfig for integration tests (no MCP tools, no remote agents)."""
    return UserConfig(
        user_id="integration-test-user",
        user_sub="integration-test-sub",
        access_token=SecretStr("fake-token-for-integration-tests"),
        name="Integration Test User",
        email="integration@test.local",
        language="en",
        timezone="Europe/Zurich",
        model=None,
        message_formatting="markdown",
        tools=[],
        sub_agents=[],
        local_subagents=[],
    )


@pytest.fixture()
def user_config_with_subagents(test_user_config):
    """Factory: a UserConfig with mock sub-agents registered and routable.

    Without this the registry is empty, so the ``task`` tool offers the model no
    ``subagent_type`` to choose and routing assertions cannot fail. Use it for
    any test about delegation::

        def test_routes_to_slack(user_config_with_subagents):
            slack = MockSubAgent("slack-client", "Sends Slack messages.")
            user_config = user_config_with_subagents(slack)

    Assignment is post-construction on purpose — it mirrors executor.py:311 and
    sidesteps the ``Runnable`` annotation on ``UserConfig.sub_agents`` that no
    real A2A runnable satisfies either.
    """

    def _make(*agents: MockSubAgent) -> UserConfig:
        test_user_config.sub_agents = mock_subagents(*agents)
        return test_user_config

    return _make


@pytest.fixture(scope="session")
def patched_graph_factory(memory_checkpointer, memory_store):
    """Create a GraphFactory with in-memory checkpointer and store.

    Session-scoped so graphs are cached across tests (they are expensive to create).
    The checkpointer and store are also session-scoped.

    The substitution recipe itself lives in ``tests.support.graph_harness`` and is
    shared with the mock tier: it pokes private attributes, so a second copy would
    drift from this one.
    """
    return patched_factory(checkpointer=memory_checkpointer, store=memory_store)


@pytest.fixture(scope="session")
def patched_agent(patched_graph_factory):
    """Create an OrchestratorDeepAgent with patched GraphFactory.

    Session-scoped so the agent (and its graphs) are reused across tests.

    Built with ``__new__`` to skip the real ``__init__`` (which would construct an
    OIDC client and live discovery services). The cost is that every attribute
    ``__init__`` assigns must be set here too — miss one and it surfaces as an
    ``AttributeError`` from deep inside ``stream()``. Keep this list in step with
    ``OrchestratorDeepAgent.__init__``.
    """
    agent = OrchestratorDeepAgent.__new__(OrchestratorDeepAgent)
    agent.config = AgentSettings()
    # Any registered chat alias; individual tests override via config metadata.
    agent._default_model_type = ALL_MODELS[0] if ALL_MODELS else None
    agent._default_thinking_level = None
    agent._graph_factory = patched_graph_factory

    # Mock discovery services (no real tool/agent discovery in integration tests)
    agent.tool_discovery_service = MagicMock()
    agent.agent_discovery_service = MagicMock()

    # Stub OAuth2 client (not needed for streaming tests)
    agent.oauth2_client = MagicMock()

    # No sandbox in tests: build_runtime_context reads this on every stream().
    agent.sandbox_pool = None

    return agent


@pytest.fixture()
def make_config(context_id, memory_checkpointer, usage_recorder):
    """Factory fixture that creates a graph config dict for a given model type.

    Carries the test's ``UsageRecorder`` in ``callbacks``: LangChain propagates
    those down to the model call, so token accounting needs no hook in
    production code.

    KNOWN GAP: three tests build their config inline instead of calling this, so
    their tokens are not counted and show as "-" in the report —
    test_concurrent_streams_isolated, test_model_switching_within_conversation
    and test_multiturn_context_preservation. They do it because they need
    thread_ids this factory does not offer: two *different* ones for concurrent
    streams, and one *shared* across invocations for the model switch. Adding an
    optional ``thread_id`` argument here would let them use it and close the gap.
    """

    def _make(model_type: ModelType, thinking_level: ThinkingLevel | None = None) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": context_id,
                "__pregel_checkpointer": memory_checkpointer,
            },
            "metadata": {
                "assistant_id": "integration-test-user",
                "user_id": "integration-test-user",
                "conversation_id": context_id,
                "user_name": "Integration Test",
                "model_type": model_type,
                "thinking_level": thinking_level,
            },
            "tags": ["integration-test"],
            "callbacks": [usage_recorder],
        }

    return _make


# ---------------------------------------------------------------------------
# Pass-ratio gating and cost reporting
# ---------------------------------------------------------------------------
# These hooks are defined in this conftest but pytest calls them for the whole
# session, so every one filters to this directory. Unit tests are deterministic
# and must not be judged on a ratio.

_EVAL = EvalSession()


def _is_integration(nodeid: str) -> bool:
    return "tests/integration/" in nodeid.replace("\\", "/")


@pytest.fixture()
def usage_recorder(request):
    """Per-test token accounting, reachable from the terminal summary."""
    recorder = UsageRecorder()
    request.node.stash_usage_recorder = recorder  # type: ignore[attr-defined]
    yield recorder
    record = _EVAL.record_for(request.node.nodeid)
    record.input_tokens += recorder.input_tokens
    record.output_tokens += recorder.output_tokens
    record.unattributed_calls += recorder.unattributed_calls


def pytest_runtest_logreport(report):
    """Collect outcome and duration for the tests under gating.

    Every failure is also noted, in any phase and any directory, because
    ``pytest_sessionfinish`` may only downgrade an exit status the ratio can
    speak for. A setup error produces no call report at all, so without this it
    would never be seen by the gate.
    """
    if report.failed:
        _EVAL.note_problem(report.nodeid, report.when)

    if report.when != "call" or not _is_integration(report.nodeid):
        return
    record = _EVAL.record_for(report.nodeid)
    record.outcome = report.outcome
    record.duration = report.duration
    record.strict = "strict" in getattr(report, "keywords", {})


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print the per-test cost table and the aggregate ratio."""
    lines = _EVAL.summary_lines()
    if not lines:
        return

    terminalreporter.write_sep("=", "integration test report")
    for line in lines:
        terminalreporter.write_line(line)

    artifact = _EVAL.write_artifact()
    if artifact:
        terminalreporter.write_line(f"report written to {artifact}")

    reason = _EVAL.gate_failure_reason()
    unaccounted = _EVAL.unaccounted_problems
    if reason:
        terminalreporter.write_line(f"GATE FAILED: {reason}")
    elif unaccounted:
        # The ratio passed but the run stays red. Say why, or this looks like a
        # bug in the gate rather than the point of it.
        terminalreporter.write_line(
            f"GATE PASSED on ratio, but {len(unaccounted)} failure(s) are outside it "
            "— fixture errors never reach the call phase, and non-integration tests "
            "are not sampled. Exit status left as-is:"
        )
        for nodeid in unaccounted:
            terminalreporter.write_line(f"  {nodeid} ({_EVAL.problems[nodeid]})")
    elif _EVAL.failed:
        # Say this loudly. A green run that contains failures is surprising, and
        # silence here would look like the failures were never noticed.
        terminalreporter.write_line(
            f"GATE PASSED with {len(_EVAL.failed)} failure(s) — "
            f"ratio {_EVAL.pass_ratio:.0%} met the {min_pass_ratio():.0%} threshold. "
            "Individual failures above are still real; investigate before assuming flakiness."
        )


def pytest_sessionfinish(session, exitstatus):
    """Decide the exit status from the aggregate ratio, not individual failures.

    Only takes over when the run was actually judging integration tests, so a
    normal unit-test run keeps pytest's own exit status untouched.

    Downgrading a red run to green is the dangerous direction, so it needs more
    than a passing ratio: every failure in the session must be one the ratio
    actually sampled. A fixture error never reaches the call phase, and a mixed
    run can contain deterministic unit failures — neither is sampling noise, and
    absolving them would turn a real regression green in CI. See
    ``EvalSession.unaccounted_problems``.
    """
    if not _EVAL.judged:
        return

    if _EVAL.gate_failure_reason():
        session.exitstatus = 1
        return

    # Only ever override a plain test-failure status — leave collection errors,
    # interrupts and internal errors alone.
    if exitstatus == 1 and _EVAL.may_downgrade_exit_status():
        session.exitstatus = 0


def available_models() -> list[ModelType]:
    """Models the gateway currently serves."""
    return list(ALL_MODELS)


def models_sharing_a_provider() -> list[tuple[ModelType, ModelType]]:
    """Pairs of distinct models on the same provider, for model-switch tests.

    Derived from the registry rather than hardcoded, so it follows whatever the
    gateway actually serves. Empty when no provider offers two models — callers
    should skip rather than quietly pass on an empty loop.
    """
    by_provider: dict[str, list[ModelType]] = {}
    for model in ALL_MODELS:
        try:
            provider = get_model_provider(model) or "unknown"
        except Exception:
            provider = "unknown"
        by_provider.setdefault(provider, []).append(model)
    return [(models[0], models[1]) for models in by_provider.values() if len(models) >= 2]


def one_model_per_provider() -> list[ModelType]:
    """One model per backing provider — a cheap smoke set across the fleet.

    Provider comes from the gateway's model_info rather than the alias string:
    Vertex also hosts Claude and Llama, so name-matching would mis-group them.
    Models whose provider cannot be resolved are grouped under "unknown" so they
    are still covered exactly once instead of silently dropped.
    """
    seen: set[str] = set()
    result: list[ModelType] = []
    for model in ALL_MODELS:
        try:
            provider = get_model_provider(model) or "unknown"
        except Exception:
            provider = "unknown"
        if provider not in seen:
            seen.add(provider)
            result.append(model)
    return result
