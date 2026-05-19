"""Shared HITL (Human-In-The-Loop) guard configuration for self-improvement tools.

This is the single source of truth for which tools require user approval.
Both the orchestrator and sub-agents import from here to ensure consistency.
"""

# Base HITL guards for all self-improvement tools.
# These apply to both the orchestrator and sub-agents.
SELF_IMPROVEMENT_HITL_GUARDS: dict[str, dict] = {
    "console_create_skill": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to create a new skill.",
    },
    "console_update_skill": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to update a skill.",
    },
    "console_remove_skill": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to deactivate a skill.",
    },
    "console_import_skill": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to import a skill from an external source.",
    },
    "console_activate_skill": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to activate a skill from the registry.",
    },
    "console_update_playbook": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to update the playbook (AGENTS.md).",
    },
    "console_write_skill_file": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to write a file to a skill folder.",
    },
    "console_delete_skill_file": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "Agent wants to delete a file from a skill folder.",
    },
}
