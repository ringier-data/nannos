"""The integration tier must be opt-in, not merely deselected by default.

``-m "not integration"`` in addopts is a *default*, and pytest's ``-m`` is
last-wins: any user-supplied expression replaces it. Every integration module
also carries ``slow``, so ``pytest -m slow`` selected 21 tests — all of them
real, billable LLM calls — where the previous ``--ignore`` had made that
impossible. These tests pin the predicate that closes that hole.
"""

import pytest

from tests.support.marker_gate import (
    ENV_OPT_IN,
    env_opt_in,
    expression_requires_marker,
    integration_requested,
)

# The markers a real integration item carries, from the modules themselves.
INTEGRATION_ITEM = ["integration", "slow", "asyncio"]
LANGSMITH_ITEM = ["integration", "slow", "langsmith", "asyncio"]


def requires(markexpr, item_markers=INTEGRATION_ITEM):
    return expression_requires_marker("integration", markexpr, item_markers)


class TestExpressionsThatDoNotRequestTheTier:
    """The bug: expressions that select integration items incidentally."""

    @pytest.mark.parametrize(
        "markexpr",
        [
            "slow",  # the reported case — selects this directory and nothing else
            "not unit",
            "slow and not unit",
        ],
    )
    def test_incidental_selection_is_not_a_request(self, markexpr):
        assert requires(markexpr) is False

    def test_langsmith_item_is_also_covered(self):
        # ``-m langsmith`` only ever reaches the hook for items that carry the
        # marker; asking about an item without it would be vacuous.
        assert requires("langsmith", LANGSMITH_ITEM) is False

    def test_no_expression_is_not_a_request(self):
        # Path or keyword selection: `pytest tests/integration/test_edge_cases.py`
        assert requires("") is False
        assert requires(None) is False

    def test_default_addopts_expression_is_not_a_request(self):
        # Never reaches the hook (these items are deselected), but must not
        # read as a request if it ever does.
        assert requires("not integration") is False


class TestExpressionsThatDoRequestTheTier:
    @pytest.mark.parametrize(
        "markexpr",
        [
            "integration",
            "integration and slow",
            "integration and not langsmith",
            "integration and langsmith",
        ],
    )
    def test_load_bearing_marker_is_a_request(self, markexpr):
        assert requires(markexpr) is True


class TestFailureModes:
    """Unparseable input must cost a skipped test, never a surprise bill."""

    @pytest.mark.parametrize("markexpr", ["integration and", "((", "and or"])
    def test_malformed_expression_fails_closed(self, markexpr):
        assert requires(markexpr) is False

    def test_missing_private_pytest_api_fails_closed(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def explode(name, *args, **kwargs):
            if name == "_pytest.mark.expression":
                raise ImportError("moved in some future pytest")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", explode)
        assert requires("integration") is False


class TestEnvOptIn:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_opt_in(self, value):
        assert env_opt_in({ENV_OPT_IN: value}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
    def test_everything_else_does_not(self, value):
        assert env_opt_in({ENV_OPT_IN: value}) is False

    def test_unset_does_not(self):
        assert env_opt_in({}) is False

    def test_env_flag_overrides_an_unrelated_expression(self, monkeypatch):
        monkeypatch.setenv(ENV_OPT_IN, "1")
        assert integration_requested("slow", INTEGRATION_ITEM) is True

    def test_without_the_flag_the_expression_decides(self, monkeypatch):
        monkeypatch.delenv(ENV_OPT_IN, raising=False)
        assert integration_requested("slow", INTEGRATION_ITEM) is False
        assert integration_requested("integration", INTEGRATION_ITEM) is True
