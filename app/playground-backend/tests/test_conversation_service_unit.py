from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from exceptions import ConversationOwnershipError

from backend.services.conversation_service import ConversationService


@pytest.mark.asyncio
async def test_get_or_create_conversation_ownership_check(monkeypatch):
    cs = ConversationService.__new__(ConversationService)
    cs.conversation_ttl_seconds = 7776000

    # Mock get_conversation to return a conversation owned by other user
    existing = MagicMock()
    existing.conversation_id = 'c1'
    existing.user_id = 'owner-123'
    existing.started_at = datetime.now(timezone.utc)
    existing.last_message_at = existing.started_at

    cs.get_conversation = AsyncMock(return_value=existing)

    with pytest.raises(ConversationOwnershipError):
        await cs.get_or_create_conversation(conversation_id='c1', user_id='attacker', agent_url='', message=None)
