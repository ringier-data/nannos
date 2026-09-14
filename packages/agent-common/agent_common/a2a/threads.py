"""Checkpoint-thread conventions shared by every process that runs a local sub-agent graph.

A local sub-agent's memory is its LangGraph checkpoint thread. Three processes
write to such threads — the orchestrator (delegation), agent-runner (scheduled
runs) and the orchestrator's embedded execute-only path — and cross-service
continuity (a conversation adopting a scheduled run, see ``conversation-origin``
in ``agent_common.a2a.extensions``) only works when all three spell the thread
the same way. This module is that spelling.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage


def local_sub_agent_thread_id(context_id: str, agent_name: str) -> str:
    """The checkpoint thread of ``agent_name``'s conversation under A2A context ``context_id``.

    One thread per (context, agent): several agents share one conversation, so
    the agent name is part of the key; the ``dynamic-`` prefix tells these
    threads apart from the orchestrator's own (bare context id) thread and from
    the built-in sub-agents' ``{ctx}::{name}`` threads in the same tables.

    Both the orchestrator's dispatch and agent-runner's scheduled execution go
    through here, which is what lets a run's conversation be continued by a later
    delegation without copying checkpoints around.
    """
    return f"{context_id}::dynamic-{agent_name}"


def seal_dangling_tool_calls(messages: list) -> list[ToolMessage]:
    """Synthetic results for AI tool calls that never got a ToolMessage.

    A run that died mid-tool (exception, timeout, pod restart) commits its last
    checkpoint right after the model emitted ``tool_calls`` — the tool results
    never landed. Appending the next human message to that history would send
    ``tool_use`` with no ``tool_result``, which providers reject outright.
    Sealing the gap with an explicit "never completed" result keeps the history
    valid AND tells the model the truth about what happened to those calls.

    Returns only the seals, in message order; the caller decides where they go
    (prepended to the next turn's input, so the checkpoint itself is never
    edited in place).
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    seals: list[ToolMessage] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tool_call in msg.tool_calls or []:
            call_id = tool_call.get("id")
            if call_id and call_id not in answered:
                answered.add(call_id)
                seals.append(
                    ToolMessage(
                        content="(no result: the previous run ended before this tool call completed)",
                        tool_call_id=call_id,
                        status="error",
                    )
                )
    return seals
