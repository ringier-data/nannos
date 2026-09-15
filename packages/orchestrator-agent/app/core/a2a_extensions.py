"""A2A protocol extension URIs and message builders — re-exported from agent-common.

The vocabulary moved to ``agent_common.a2a.extensions`` so that every A2A server
in the repo emits it: the orchestrator over HTTP, agent-runner, and the
in-process server every local sub-agent runs behind. This module keeps the
orchestrator's import path (and the conformance test that pins ``ALL_EXTENSIONS``
to the repo-root ``a2a-extensions.json``) unchanged.
"""

from agent_common.a2a.extensions import (
    ACTIVITY_LOG_EXTENSION,
    ALL_EXTENSIONS,
    CLIENT_ACTION_EXTENSION,
    CONVERSATION_ORIGIN_EXTENSION,
    FEEDBACK_REQUEST_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    IN_TASK_AUTH_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
    new_activity_log_message,
    new_auth_required_message,
    new_client_action_message,
    new_client_action_request_message,
    new_feedback_request_message,
    new_hitl_interrupt_message,
    new_work_plan_message,
)

__all__ = [
    "ACTIVITY_LOG_EXTENSION",
    "ALL_EXTENSIONS",
    "CLIENT_ACTION_EXTENSION",
    "CONVERSATION_ORIGIN_EXTENSION",
    "FEEDBACK_REQUEST_EXTENSION",
    "HUMAN_IN_THE_LOOP_EXTENSION",
    "IN_TASK_AUTH_EXTENSION",
    "INTERMEDIATE_OUTPUT_EXTENSION",
    "WORK_PLAN_EXTENSION",
    "new_activity_log_message",
    "new_auth_required_message",
    "new_client_action_message",
    "new_client_action_request_message",
    "new_feedback_request_message",
    "new_hitl_interrupt_message",
    "new_work_plan_message",
]
