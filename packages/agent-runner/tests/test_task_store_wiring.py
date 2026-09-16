"""Both A2A task stores must be the durable one, not just the sub-agent's.

A parked run leaves TWO task records behind, and they are not interchangeable:

* the **outer** task, owned by the HTTP request handler — the one a parked run leaves
  non-terminal and the one the owner's authorization answer is addressed to by id;
* the **inner** task, owned by ``LocalA2AServer`` for the sub-agent itself.

Installing the durable store on only the inner one looks correct for as long as the
process lives. It fails at exactly the moment ADR-0009 decision 3 exists to survive: a
restart between the park and the answer. The sub-agent's task is found in Postgres, the
outer task is gone with the memory it lived in, and the resume dies on
``Task ... not found`` — leaving the job stopped for good, since a parked run holds its
schedule. agent-runner is the service that gets OOMKilled, so this is the ordinary case.
"""

from __future__ import annotations

import ast
import pathlib


def _create_app_source() -> str:
    source = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text()
    tree = ast.parse(source)
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    return ast.get_source_segment(source, fn) or ""


class TestBothStoresAreDurable:
    def test_the_http_handler_does_not_get_an_in_memory_store(self):
        """The outer task must outlive the process that parked it."""
        src = _create_app_source()
        assert "InMemoryTaskStore" not in src, (
            "the HTTP request handler is back on an in-memory task store: a run parked "
            "before a restart can no longer be answered"
        )

    def test_the_http_handler_and_the_sub_agent_server_share_one_store(self):
        src = _create_app_source()
        assert "task_store, _task_store_engine = create_task_store()" in src
        # The same object on both sides — two stores would put the outer and inner tasks
        # in different tables and only one of them would survive.
        assert "set_local_task_store(task_store)" in src
        assert "task_store=task_store," in src


class TestDurabilityIsGatedTheSameWayTheCheckpointerIs:
    """A password must not decide whether the task store is durable.

    ``core.py``'s checkpointer asks only for ``POSTGRES_HOST``. If the store asked for a
    password too, an IAM / trust / .pgpass deployment would get a durable checkpointer
    beside a volatile store — the split ADR-0009 decision 3 forbids. It fails only at the
    moment it matters: a restart between the park and the answer leaves the graph
    resumable and the task it is addressed by gone.
    """

    def test_a_passwordless_host_still_gets_the_durable_store(self, monkeypatch):
        from agent import task_store as ts

        monkeypatch.setenv("POSTGRES_HOST", "db.internal")
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_SCHEMA", raising=False)

        store, engine = ts.create_task_store()
        try:
            assert engine is not None, "a passwordless host fell back to the in-memory store"
            assert store.__class__.__name__ == "DatabaseTaskStore"
        finally:
            if engine is not None:
                engine.sync_engine.dispose()

    def test_no_host_is_the_only_in_memory_case(self, monkeypatch):
        from agent import task_store as ts

        monkeypatch.delenv("POSTGRES_HOST", raising=False)
        store, engine = ts.create_task_store()
        assert engine is None
        assert store.__class__.__name__ == "InMemoryTaskStore"

    def test_the_configured_schema_pins_the_search_path(self):
        """Without this the table lands in the role default, where another replica's
        search_path may not find it."""
        from agent import task_store as ts

        assert ts.schema_connect_args("nannos_runner") == {"options": "-csearch_path=nannos_runner"}
        # Unset means "whatever the role defaults to", which is the pre-existing
        # single-environment behaviour and must not become a literal empty search_path.
        assert ts.schema_connect_args(None) == {}
        assert ts.schema_connect_args("") == {}
