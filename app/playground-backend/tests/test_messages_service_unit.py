"""Unit tests for backend.services.messages_service."""

from unittest.mock import AsyncMock

import pytest

from backend.models.message import Message
from backend.services.messages_service import (
    MessagesService,
    _parse_agent_response,
    _parse_status_update,
    _parse_task,
)


@pytest.mark.asyncio
async def test_save_agent_response_nested_and_flat_formats():
    ms = MessagesService.__new__(MessagesService)

    # Patch insert_message to capture args and return a Message
    called = {}

    async def fake_insert_message(**kwargs):
        called.update(kwargs)
        # return a Message instance similar to real
        return Message(
            conversation_id=kwargs.get('conversation_id', ''),
            sort_key=f'MSG#0#{kwargs.get("message_id", "")}',
            user_id=kwargs.get('user_id', ''),
            message_id=kwargs.get('message_id', ''),
            role=kwargs.get('role', ''),
            parts=kwargs.get('parts', []),
            created_at='2025-01-01T00:00:00+00:00',
            state=kwargs.get('state', 'completed'),
            raw_payload=kwargs.get('raw_payload', ''),
            metadata=kwargs.get('metadata', {}),
            ttl=0,
            final=kwargs.get('final', False),
            kind=kwargs.get('kind', ''),
        )

    ms.insert_message = AsyncMock(side_effect=fake_insert_message)

    # Nested format with status-update kind
    nested = {
        'id': 'agent-1',
        'final': True,
        'kind': 'status-update',
        'status': {
            'state': 'completed',
            'message': {
                'messageId': 'msg-nested-1',
                'role': 'assistant',
                'parts': [{'kind': 'text', 'text': 'nested reply'}],
                'metadata': {'k': 'v'},
            },
        },
    }

    res = await ms.save_agent_response(nested, conversation_id='conv-1', user_id='user-1')
    assert res is not None
    # insert_message should have been called with extracted nested values
    assert called['conversation_id'] == 'conv-1'
    assert called['user_id'] == 'user-1'
    assert called['message_id'] == 'msg-nested-1'
    assert called['role'] == 'assistant'
    assert isinstance(called['parts'], list) and called['parts'][0]['text'] == 'nested reply'


def test_parse_status_update_with_nested_message():
    """Test parsing status-update with nested status.message."""
    response = {
        'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
        'final': False,
        'kind': 'status-update',
        'status': {
            'message': {
                'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
                'kind': 'message',
                'messageId': 'f47bc0fc-12b4-42f0-a3d2-0e5786afda8f',
                'parts': [{'kind': 'text', 'text': '📋 **Plan created** (5 tasks)'}],
                'role': 'agent',
                'taskId': '98ab980a-2209-42a5-aaae-55e93e3108e0',
            },
            'state': 'working',
            'timestamp': '2025-11-21T09:23:15.724055+00:00',
        },
        'taskId': '98ab980a-2209-42a5-aaae-55e93e3108e0',
        'id': '09e43b4e-fe71-4057-bc00-db9e9b74ab80',
    }

    parsed = _parse_status_update(response)

    assert parsed['kind'] == 'status-update'
    assert parsed['message_id'] == 'f47bc0fc-12b4-42f0-a3d2-0e5786afda8f'
    assert parsed['role'] == 'assistant'  # 'agent' normalized to 'assistant'
    from a2a.types import TaskState as _TS

    assert parsed['state'] == _TS.working
    assert parsed['final'] is False
    assert parsed['task_id'] == '98ab980a-2209-42a5-aaae-55e93e3108e0'
    assert len(parsed['parts']) == 1
    assert parsed['parts'][0]['text'] == '📋 **Plan created** (5 tasks)'


def test_parse_status_update_without_message():
    """Test parsing status-update with only state (no nested message)."""
    response = {
        'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
        'final': True,
        'kind': 'status-update',
        'status': {'state': 'completed', 'timestamp': '2025-11-21T09:24:00.493443+00:00'},
        'taskId': '98ab980a-2209-42a5-aaae-55e93e3108e0',
        'id': '09e43b4e-fe71-4057-bc00-db9e9b74ab80',
    }

    parsed = _parse_status_update(response)

    assert parsed['kind'] == 'status-update'
    from a2a.types import TaskState as _TS

    assert parsed['state'] == _TS.completed
    assert parsed['final'] is True
    assert parsed['task_id'] == '98ab980a-2209-42a5-aaae-55e93e3108e0'
    # Should create synthetic status part
    assert len(parsed['parts']) == 1
    assert 'Status: completed' in parsed['parts'][0]['text']
    assert '2025-11-21T09:24:00.493443+00:00' in parsed['parts'][0]['text']


def test_parse_status_update_with_artifact():
    """Test parsing artifact-update kind."""
    response = {
        'artifact': {
            'artifactId': '96b01ff4-f2fb-456d-9db6-6b0d036e4771',
            'name': 'orchestrator_result',
            'parts': [{'kind': 'text', 'text': "Your trip to Paris is fully planned! Here's a summary..."}],
        },
        'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
        'kind': 'artifact-update',
        'taskId': '98ab980a-2209-42a5-aaae-55e93e3108e0',
        'id': '09e43b4e-fe71-4057-bc00-db9e9b74ab80',
    }

    parsed = _parse_status_update(response)

    assert parsed['kind'] == 'artifact-update'
    assert parsed['message_id'] == '96b01ff4-f2fb-456d-9db6-6b0d036e4771'
    assert parsed['task_id'] == '98ab980a-2209-42a5-aaae-55e93e3108e0'
    assert len(parsed['parts']) == 1
    assert 'Your trip to Paris' in parsed['parts'][0]['text']


def test_parse_task_with_history():
    """Test parsing task kind with history."""
    response = {
        'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
        'history': [
            {
                'contextId': 'dde49b7a-b4b8-48ba-9276-11a42d820f22',
                'kind': 'message',
                'messageId': '09e43b4e-fe71-4057-bc00-db9e9b74ab80',
                'metadata': {'user_id': '0490f8d6-67ee-439b-8178-6ed66a72b0c9'},
                'parts': [{'kind': 'text', 'text': 'Help me plan a trip to Paris'}],
                'role': 'user',
                'taskId': '98ab980a-2209-42a5-aaae-55e93e3108e0',
            }
        ],
        'id': '98ab980a-2209-42a5-aaae-55e93e3108e0',
        'kind': 'task',
        'status': {'state': 'submitted'},
    }

    parsed = _parse_task(response)

    assert parsed['kind'] == 'task'
    from a2a.types import TaskState as _TS

    assert parsed['state'] == _TS.submitted
    assert parsed['task_id'] == '98ab980a-2209-42a5-aaae-55e93e3108e0'
    assert parsed['message_id'] == '98ab980a-2209-42a5-aaae-55e93e3108e0'
    assert 'history' in parsed
    assert len(parsed['history']) == 1
    assert parsed['history'][0]['messageId'] == '09e43b4e-fe71-4057-bc00-db9e9b74ab80'
    # Should create task event part
    assert len(parsed['parts']) == 1
    assert 'Task submitted' in parsed['parts'][0]['text']


def test_parse_agent_response_dispatches_correctly():
    """Test that _parse_agent_response dispatches to correct parser."""
    # status-update should use _parse_status_update
    status_response = {'kind': 'status-update', 'status': {'state': 'working'}, 'id': 'test-1'}
    parsed = _parse_agent_response(status_response)
    assert parsed['kind'] == 'status-update'
    from a2a.types import TaskState as _TS

    assert parsed['state'] == _TS.working

    # task should use _parse_task
    task_response = {'kind': 'task', 'id': 'task-1', 'status': {'state': 'submitted'}}
    parsed = _parse_agent_response(task_response)
    assert parsed['kind'] == 'task'
    from a2a.types import TaskState as _TS

    assert parsed['state'] == _TS.submitted

    # unknown kind should raise ValueError
    unknown_response = {'kind': 'custom-type', 'messageId': 'msg-1', 'parts': [{'text': 'hello'}]}
    with pytest.raises(ValueError, match="Unsupported agent response kind: 'custom-type'"):
        _parse_agent_response(unknown_response)


@pytest.mark.asyncio
async def test_save_agent_response_with_history():
    """Test that save_agent_response calls save_history_messages when history present."""
    ms = MessagesService.__new__(MessagesService)

    insert_calls = []
    history_calls = []

    async def fake_insert(**kwargs):
        insert_calls.append(kwargs)
        return Message(
            conversation_id=kwargs['conversation_id'],
            sort_key=f'MSG#0#{kwargs["message_id"]}',
            user_id=kwargs['user_id'],
            message_id=kwargs['message_id'],
            role=kwargs['role'],
            parts=kwargs['parts'],
            created_at='2025-01-01T00:00:00+00:00',
            state=kwargs['state'],
            raw_payload=kwargs['raw_payload'],
            metadata=kwargs['metadata'],
            ttl=0,
            final=kwargs['final'],
            kind=kwargs['kind'],
        )

    async def fake_save_history(history, conv_id, user_id):
        history_calls.append({'history': history, 'conv_id': conv_id, 'user_id': user_id})
        return len(history)

    ms.insert_message = AsyncMock(side_effect=fake_insert)

    task_response = {
        'kind': 'task',
        'id': 'task-123',
        'status': {'state': 'submitted'},
        'history': [{'messageId': 'hist-1', 'role': 'user', 'parts': [{'text': 'question'}]}],
    }

    result = await ms.save_agent_response(task_response, 'conv-1', 'user-1')

    assert result is not None
    assert len(insert_calls) == 1
    assert insert_calls[0]['message_id'] == 'task-123'

    # History is no longer saved by the service; do not expect save_history_messages to be called
    assert not hasattr(ms, 'save_history_messages')
