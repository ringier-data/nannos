"""Tests for the A2A task store factory."""

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore

from app.core.task_store import create_task_store
from app.models.config import AgentSettings


def test_falls_back_to_in_memory_without_postgres(monkeypatch):
    monkeypatch.setattr(AgentSettings, "POSTGRES_PASSWORD", "")

    store, engine = create_task_store()

    assert isinstance(store, InMemoryTaskStore)
    assert engine is None


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
