"""Tests for the A2A task store factory."""

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore

from app.core.task_store import create_task_store
from app.models.config import AgentSettings


def test_falls_back_to_in_memory_without_postgres(monkeypatch):
    """No HOST is the only in-memory case."""
    monkeypatch.setattr(AgentSettings, "POSTGRES_HOST", "")

    store, engine = create_task_store()

    assert isinstance(store, InMemoryTaskStore)
    assert engine is None


def test_a_passwordless_host_still_gets_the_durable_store(monkeypatch):
    """The gate is POSTGRES_HOST alone, as it is for the checkpointer.

    It used to be host AND password, which split a deployment authenticating by IAM,
    trust or ``.pgpass``: a durable checkpointer beside a volatile task store — the
    configuration ADR-0009 decision 3 says must not exist. It fails only at the moment
    the store exists for, when a restart leaves a graph resumable and the task it is
    addressed by gone. Changed when this factory was shared with agent-runner, which had
    already been fixed; sharing the code is what made the disagreement visible.
    """
    monkeypatch.setattr(AgentSettings, "POSTGRES_HOST", "db.example.com")
    monkeypatch.setattr(AgentSettings, "POSTGRES_PASSWORD", "")

    store, engine = create_task_store()

    assert isinstance(store, DatabaseTaskStore)
    assert engine is not None


def test_the_orchestrator_keeps_its_own_table_and_schema_resolution(monkeypatch):
    """Neither knob the shared factory offers is set here, and both omissions are the point.

    ``table_name`` unset keeps the SDK default ``tasks`` table this service already owns;
    agent-runner is the one that had to name its own, so neither can reach the other's
    tasks. ``schema`` unset keeps the table wherever the role's search_path resolves it
    today — pinning it would not move the existing table, it would create an empty one
    elsewhere and hide the tasks this service already has.
    """
    monkeypatch.setattr(AgentSettings, "POSTGRES_HOST", "db.example.com")

    _, engine = create_task_store()

    assert engine.url.database == AgentSettings.POSTGRES_DB
    # No search_path pinned onto the connection.
    assert "options" not in (engine.sync_engine.pool._dialect.create_connect_args(engine.url)[1] or {})


def test_uses_database_store_when_postgres_configured(monkeypatch):
    monkeypatch.setattr(AgentSettings, "POSTGRES_HOST", "db.example.com")
    monkeypatch.setattr(AgentSettings, "POSTGRES_PASSWORD", "secret")

    # Engine creation is lazy: no connection is opened until first use,
    # so this is safe without a running database.
    store, engine = create_task_store()

    assert isinstance(store, DatabaseTaskStore)
    assert engine is not None
    assert engine.url.host == "db.example.com"
    assert engine.url.drivername == "postgresql+psycopg"


def test_password_with_special_characters_is_preserved(monkeypatch):
    monkeypatch.setattr(AgentSettings, "POSTGRES_HOST", "db.example.com")
    monkeypatch.setattr(AgentSettings, "POSTGRES_PASSWORD", "p@ss:w/rd%40")

    _, engine = create_task_store()

    assert engine.url.password == "p@ss:w/rd%40"
