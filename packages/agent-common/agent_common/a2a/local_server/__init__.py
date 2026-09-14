"""The in-process A2A server every local sub-agent runs behind.

A local sub-agent is a LangGraph graph in the same process as its caller. It
used to be driven through a parallel implementation of the task lifecycle —
the caller probed the graph's checkpoint for pending interrupts, caught the
``GraphInterrupt`` the graph re-raised, built the ``Command(resume)`` itself,
and read results out of a JSON envelope. Remote agents, meanwhile, got all of
that from the A2A protocol. This package closes the gap: a local sub-agent is
served by the same A2A SDK machinery a remote one is (``AgentExecutor`` +
``DefaultRequestHandler`` + ``TaskStore``), minus the HTTP hop.

- :class:`LocalSubAgentExecutor` runs the graph for one A2A message: fresh
  task, follow-up on the agent's conversation, or the answer to a paused task
  (which it turns into the id-keyed ``Command(resume)`` LangGraph wants).
- :class:`LocalA2AServer` owns the request handler and the per-conversation
  serialisation, and is what a runnable's ``astream`` talks to.
- :class:`ProposedTaskIdContextBuilder` lets the caller name a new task after
  the delegating tool call, so a replayed call finds the task it opened.
- :mod:`.resume` fits a client's answer to the question the graph is paused on.

The task store is shared process-wide (:func:`set_local_task_store`); the
orchestrator installs its persistent store at startup so local tasks survive a
restart like remote ones do.
"""

from .context_builder import ProposedTaskIdContextBuilder
from .executor import PARENT_CONFIG_KEY, PARKED_TASK_MESSAGE, LocalSubAgentExecutor
from .resume import ANSWER_NOTHING, align_answer_to_interrupt, build_resume_command, replicate_blanket_decision
from .server import LocalA2AServer, get_local_task_store, set_local_task_store

__all__ = [
    "ANSWER_NOTHING",
    "PARENT_CONFIG_KEY",
    "PARKED_TASK_MESSAGE",
    "LocalA2AServer",
    "LocalSubAgentExecutor",
    "ProposedTaskIdContextBuilder",
    "align_answer_to_interrupt",
    "build_resume_command",
    "get_local_task_store",
    "replicate_blanket_decision",
    "set_local_task_store",
]
