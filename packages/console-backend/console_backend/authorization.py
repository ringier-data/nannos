"""Authorization constants and helpers for role-based access control."""

# System role capabilities define what actions each role can perform system-wide
# Used by check_user_permission() for system-level authorization
#
# Action semantics:
# - Plain actions (read, write): Always use group permission intersection
# - .admin suffix actions: Bypass group intersection, require admin-mode enabled
# - approve: Approve submissions in accessible groups (intersection applies, requires admin-mode)
# - approve.admin: Approve ANY submission system-wide (bypasses intersection, requires admin-mode)
SYSTEM_ROLE_CAPABILITIES = {
    "member": {
        "groups": {"read"},  # View groups they're in (intersection applies)
        "members": {"read", "write"},  # View/manage members (intersection applies)
        "sub_agents": {"read", "write"},  # View/manage sub-agents (intersection applies)
        "secrets": {"read", "write"},  # View/manage secrets (intersection applies)
        "catalogs": {"read", "write"},  # View/manage catalogs (intersection applies)
        "bug_reports": {"read", "write"},  # View/resolve own bug reports (no intersection)
    },
    "approver": {
        "groups": {"read"},  # View groups they're in (intersection applies)
        "members": {"read", "write"},  # View/manage members (intersection applies)
        "sub_agents": {
            "read",
            "write",
            "approve",  # Approve sub-agents in accessible groups (intersection applies, requires admin-mode)
        },
        "secrets": {"read", "write"},  # View/manage secrets (intersection applies)
        "catalogs": {"read", "write"},  # View/manage catalogs (intersection applies)
        "bug_reports": {"read", "triage"},  # View own reports, triage any accessible (no intersection)
    },
    "admin": {
        "groups": {
            "read",
            "read.admin",  # View all groups system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # CRUD all groups system-wide (bypasses intersection, requires admin-mode)
        },
        "members": {
            "read",
            "write",
            "read.admin",  # View all members system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Manage all members system-wide (bypasses intersection, requires admin-mode)
        },
        "users": {
            "read.admin",  # View all users system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Update user status, roles (bypasses intersection, requires admin-mode)
        },
        "sub_agents": {
            "read",
            "write",
            "read.admin",  # View all sub-agents system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Modify all sub-agents system-wide (bypasses intersection, requires admin-mode)
            "approve.admin",  # Approve ANY sub-agent system-wide (bypasses intersection, requires admin-mode)
        },
        "secrets": {
            "read",
            "write",
            "read.admin",  # View all secrets system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Modify all secrets system-wide (bypasses intersection, requires admin-mode)
        },
        "catalogs": {
            "read",
            "write",
            "read.admin",  # View all catalogs system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Modify all catalogs system-wide (bypasses intersection, requires admin-mode)
        },
        "bug_reports": {
            "read",
            "write",
            "read.admin",  # View all bug reports system-wide (bypasses intersection, requires admin-mode)
            "write.admin",  # Modify all bug reports system-wide (bypasses intersection, requires admin-mode)
            "triage.admin",  # Triage any bug report system-wide (bypasses intersection, requires admin-mode)
        },
    },
}

# Group role capabilities define what actions a user with this role can perform on resources
# within a group. This is intersected with resource-level permissions.
#
# Authorization model: effective_permissions = resource_permissions ∩ role_capabilities
# Example: User with 'read' role in a group that has ['read', 'write'] on a sub-agent
#          → User can only perform 'read' actions
GROUP_ROLE_CAPABILITIES = {
    "read": {
        "sub_agents": {"read"},  # Read-only access to sub-agents
        "members": {"read"},  # Can view group members
        "secrets": {"read"},  # Read-only access to secrets
        "catalogs": {"read"},  # Read-only access to catalogs
    },
    "write": {
        "sub_agents": {"read", "write"},  # Full sub-agent access
        "members": {"read"},  # Can view group members
        "secrets": {"read"},  # Read access to secrets
        "catalogs": {"read", "write"},  # Read/write access to catalogs
    },
    "manager": {
        "sub_agents": {"read", "write"},  # Full sub-agent access
        "members": {"read", "write"},  # Can view and manage group membership
        "secrets": {"read", "write"},  # Read/write access to secrets
        "catalogs": {"read", "write"},  # Read/write access to catalogs
    },
}


def check_action_allowed(group_role: str, resource_type: str, action: str) -> bool:
    """Check if a group role allows a specific action on a resource type.

    Args:
        group_role: User's role in the group ('read', 'write', 'manager')
        resource_type: Type of resource (e.g., 'sub_agents', 'members')
        action: Action to check (e.g., 'read', 'write', 'add', 'remove')

    Returns:
        True if the role allows the action
    """
    if group_role not in GROUP_ROLE_CAPABILITIES:
        return False

    role_capabilities = GROUP_ROLE_CAPABILITIES[group_role]
    if resource_type not in role_capabilities:
        return False

    return action in role_capabilities[resource_type]


def check_capability(user_role: str, resource_type: str, capability: str) -> bool:
    """Check if a user role has a specific capability on a resource type.

    Args:
        user_role: User's system role ('member', 'approver', 'admin')
        resource_type: Type of resource (e.g., 'sub_agents', 'secrets', 'members')
        capability: Capability to check (e.g., 'read', 'write', 'approve', 'read.admin')

    Returns:
        True if the user role has the capability

    Examples:
        >>> check_capability('member', 'secrets', 'read.own')
        True  # Members can read their own secrets
        >>> check_capability('member', 'secrets', 'read.group')
        False  # Members cannot read group secrets
        >>> check_capability('admin', 'secrets', 'read.admin')
        True  # Admins can read any secret in the system
    """
    if user_role not in SYSTEM_ROLE_CAPABILITIES:
        return False

    role_capabilities = SYSTEM_ROLE_CAPABILITIES.get(user_role, {})
    if resource_type not in role_capabilities:
        return False

    return capability in role_capabilities[resource_type]
