"""Let a client name the task a new message opens.

A2A servers mint task ids. The orchestrator has one reason to name a task before
it exists: a delegation is a LangGraph tool call, and LangGraph replays that call
byte-identical when the orchestrator resumes from an interrupt. If the first
attempt opened a task that is now waiting for a decision, the replay must find
*that* task — not open a second one and run the work twice. Deriving the task id
from the tool call (``uuid5`` of conversation + tool-call id) and proposing it on
the message makes the replay's ``tasks/get`` land on the parked task.

The proposal is honoured only for a message that opens a new task, and only when
no task by that id exists yet: a proposal that collides with a stored task (a
provider reusing tool-call ids, a replayed test) is dropped and the server mints
an id as usual — the id is a convenience for the caller, never a correctness
requirement for the server. A message continuing an existing task is addressed
by ``message.task_id`` as usual.
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import SendMessageRequest, Task
from google.protobuf.json_format import MessageToDict

from ..message_conversion import PROPOSED_TASK_ID_KEY

logger = logging.getLogger(__name__)


class ProposedTaskIdContextBuilder(SimpleRequestContextBuilder):
    """``SimpleRequestContextBuilder`` that adopts a proposed id for a new task."""

    def __init__(self, task_store: TaskStore | None = None) -> None:
        super().__init__(should_populate_referred_tasks=False, task_store=task_store)

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        if task is None and not task_id and params is not None and params.message.HasField("metadata"):
            proposed = MessageToDict(params.message.metadata).get(PROPOSED_TASK_ID_KEY)
            if isinstance(proposed, str) and proposed:
                taken = self._task_store is not None and await self._task_store.get(proposed, context) is not None
                if taken:
                    logger.info("Proposed task id %s already names a stored task; minting a fresh id", proposed[:8])
                else:
                    task_id = proposed
        return await super().build(context=context, params=params, task_id=task_id, context_id=context_id, task=task)
