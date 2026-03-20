"""Smoke tests for in-memory / local-filesystem service replacements."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# These env vars are required at import time by playground_backend.config
os.environ.setdefault("OIDC_ISSUER", "http://localhost:8080/realms/test")
os.environ.setdefault("OIDC_CLIENT_ID", "test")
os.environ.setdefault("OIDC_AUDIENCE", "test")

from playground_backend.services.in_memory_session_service import InMemorySessionService
from playground_backend.services.in_memory_socket_session_service import InMemorySocketSessionService
from playground_backend.services.in_memory_conversation_service import InMemoryConversationService
from playground_backend.services.in_memory_messages_service import InMemoryMessagesService
from playground_backend.services.local_file_storage_service import LocalFileStorageService


# -- InMemorySessionService --------------------------------------------------

@pytest.mark.asyncio
async def test_session_create_and_get():
    svc = InMemorySessionService()
    session_id = await svc.create_session("u1", "refresh-tok", "id-tok", "access-tok")
    assert isinstance(session_id, str)
    got = await svc.get_session(session_id)
    assert got is not None
    assert got.user_id == "u1"
    assert got.access_token == "access-tok"


@pytest.mark.asyncio
async def test_session_destroy():
    svc = InMemorySessionService()
    session_id = await svc.create_session("u1", "refresh-tok", "id-tok", "access-tok")
    await svc.destroy_session(session_id)
    assert await svc.get_session(session_id) is None


@pytest.mark.asyncio
async def test_session_update():
    svc = InMemorySessionService()
    session_id = await svc.create_session("u1", "refresh-tok", "id-tok", "access-tok")
    await svc.update_session(session_id, "u1", access_token="new-access-tok")
    got = await svc.get_session(session_id)
    assert got is not None
    assert got.access_token == "new-access-tok"


@pytest.mark.asyncio
async def test_session_orchestrator_cookie():
    svc = InMemorySessionService()
    session_id = await svc.create_session("u1", "refresh-tok", "id-tok", "access-tok")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    await svc.update_orchestrator_cookie(session_id, "cookie-value", expires)
    result = await svc.get_orchestrator_cookie(session_id)
    assert result is not None
    assert result[0] == "cookie-value"
    await svc.clear_orchestrator_cookie(session_id)
    assert await svc.get_orchestrator_cookie(session_id) is None


# -- InMemorySocketSessionService --------------------------------------------

@pytest.mark.asyncio
async def test_socket_session_create_and_get():
    svc = InMemorySocketSessionService()
    sock = await svc.create_session("sid-1", "u2", "http-sess-1")
    assert sock.user_id == "u2"
    assert sock.socket_id == "sid-1"
    got = await svc.get_session("sid-1")
    assert got is not None


@pytest.mark.asyncio
async def test_socket_session_destroy():
    svc = InMemorySocketSessionService()
    await svc.create_session("sid-2", "u2", "http-sess-1")
    await svc.destroy_session("sid-2")
    assert await svc.get_session("sid-2") is None


@pytest.mark.asyncio
async def test_socket_session_initialize_client():
    svc = InMemorySocketSessionService()
    await svc.create_session("sid-3", "u2", "http-sess-1")
    await svc.initialize_client("sid-3", "http://agent:8080", {"X-Custom": "val"})
    got = await svc.get_session("sid-3")
    assert got is not None
    assert got.agent_url == "http://agent:8080"
    assert got.is_initialized is True


# -- InMemoryConversationService ---------------------------------------------

@pytest.mark.asyncio
async def test_conversation_get_or_create():
    svc = InMemoryConversationService()
    conv = await svc.get_or_create_conversation("c1", "u3")
    assert conv.user_id == "u3"
    assert conv.conversation_id == "c1"
    # Second call returns the same conversation
    conv2 = await svc.get_or_create_conversation("c1", "u3")
    assert conv2.conversation_id == conv.conversation_id


@pytest.mark.asyncio
async def test_conversation_list_by_user():
    svc = InMemoryConversationService()
    await svc.get_or_create_conversation("c1", "u3")
    await svc.get_or_create_conversation("c2", "u3")
    await svc.get_or_create_conversation("c3", "other")
    convs = await svc.get_conversations_by_user_id("u3")
    assert len(convs) == 2


@pytest.mark.asyncio
async def test_conversation_insert_and_get():
    svc = InMemoryConversationService()
    await svc.insert_conversation("u4", title="My Chat", conversation_id="c4")
    conv = await svc.get_conversation("c4", "u4")
    assert conv is not None
    assert conv.title == "My Chat"


# -- InMemoryMessagesService -------------------------------------------------

@pytest.mark.asyncio
async def test_messages_insert_and_list():
    svc = InMemoryMessagesService()
    msg = await svc.insert_message("c1", "u4", "user", [{"type": "text", "text": "Hello"}])
    assert msg.role == "user"
    assert len(msg.parts) == 1
    msgs = await svc.get_messages_by_conversation("c1", "u4")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_messages_ordering():
    svc = InMemoryMessagesService()
    await svc.insert_message("c1", "u4", "user", [{"type": "text", "text": "First"}])
    await svc.insert_message("c1", "u4", "assistant", [{"type": "text", "text": "Second"}])
    msgs = await svc.get_messages_by_conversation("c1", "u4")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


# -- LocalFileStorageService -------------------------------------------------

@pytest.mark.asyncio
async def test_local_storage_properties():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = LocalFileStorageService(base_path=tmpdir)
        assert svc.bucket == "local"
        assert svc.presigned_ttl_seconds == 3600


def test_local_storage_allowed_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = LocalFileStorageService(base_path=tmpdir)
        assert svc.is_allowed_file("image/png")
        assert svc.is_allowed_file("text/plain")
        assert svc.is_allowed_file("audio/mpeg")
        assert svc.is_allowed_file("application/pdf")
        assert not svc.is_allowed_file("application/x-executable")


@pytest.mark.asyncio
async def test_local_storage_presigned_url():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = LocalFileStorageService(base_path=tmpdir)
        url = await svc.generate_presigned_get_url("uploads/u1/c1/file.png")
        assert url == "/api/v1/files/local/uploads/u1/c1/file.png"


@pytest.mark.asyncio
async def test_local_storage_upload_and_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = LocalFileStorageService(base_path=tmpdir)

        # Create a fake UploadFile
        import io
        from fastapi import UploadFile as UF

        content = b"fake image data"
        upload = UF(filename="test.png", file=io.BytesIO(content), headers={"content-type": "image/png"})

        result = await svc.upload_file(upload, user_id="u1", conversation_id="c1")
        assert result.bucket == "local"
        assert result.name == "test.png"
        assert result.size == len(content)
        assert result.key.startswith("uploads/u1/c1/")

        # Verify file exists on disk
        file_path = os.path.join(tmpdir, result.key)
        assert os.path.isfile(file_path)

        # Delete
        await svc.delete_file(result.key)
        assert not os.path.isfile(file_path)
