"""The mock tier compiles a real graph, so its scaffolding needs its own guard.

``patched_factory`` reaches past GraphFactory's public API — there is no injection
seam — and substitutes in-memory doubles for DynamoDB and Postgres. The failure
mode worth guarding is not a crash: it is GraphFactory renaming one of those
private attributes and these tests continuing to *pass* against a
half-initialized factory, which quietly voids the point of compiling the real
graph at all.

No LLM, no gateway.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from app.core.graph_factory import GraphFactory
from app.models.config import AgentSettings
from tests.support.graph_harness import PATCHED_ATTRIBUTES, patched_factory, scripted_graph
from tests.support.scripted_model import ScriptedChatModel


@pytest.mark.parametrize("attribute", PATCHED_ATTRIBUTES)
def test_every_patched_attribute_still_exists(attribute):
    """If GraphFactory renames one, fail here — loudly, and in one place."""
    unpatched = GraphFactory(config=AgentSettings(), cost_logger=None)

    assert hasattr(unpatched, attribute), (
        f"GraphFactory no longer has {attribute!r}. tests/support/graph_harness "
        "substitutes it, so both test tiers are now building against a "
        "half-initialized factory. Update PATCHED_ATTRIBUTES and the recipe."
    )


def test_the_doubles_are_actually_installed():
    factory = patched_factory()

    assert isinstance(factory._checkpointer, MemorySaver)
    assert isinstance(factory._store, InMemoryStore)
    assert factory._store_setup_complete is True
    assert factory._static_tools_cache  # time tool, so no backend_factory needed


def test_injected_checkpointer_and_store_are_used():
    """The seam the integration conftest needs: session-scoped doubles passed in."""
    checkpointer, store = MemorySaver(), InMemoryStore()

    factory = patched_factory(checkpointer=checkpointer, store=store)

    assert factory._checkpointer is checkpointer
    assert factory._store is store


def test_the_model_swap_is_scoped_to_the_instance():
    """Shadowed on the instance, not the class — so it cannot leak between tests."""
    model = ScriptedChatModel(responses=[])

    patched = patched_factory(model=model)
    other = patched_factory()

    assert patched._create_model() is model
    assert other._create_model is not patched._create_model


def test_a_graph_actually_compiles():
    """The whole recipe, end to end: production graph, scripted LLM, no network."""
    graph = scripted_graph(ScriptedChatModel(responses=[]))

    assert graph is not None
