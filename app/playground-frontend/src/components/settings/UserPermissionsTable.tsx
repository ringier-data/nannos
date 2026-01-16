import { Shield, Check, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useAuth } from '@/contexts/AuthContext';

// System role capabilities define what actions each role can perform system-wide
const SYSTEM_ROLE_CAPABILITIES = {
  member: {
    groups: new Set(['read']),
  },
  approver: {
    groups: new Set(['read']),
    sub_agents: new Set(['approve']), // Requires write group access to the resource
  },
  admin: {
    groups: new Set(['read', 'write', 'create']),
    users: new Set(['read', 'write']),
    sub_agents: new Set(['approve']), // Requires write group access to the resource
  },
} as const;

// Group role capabilities define what actions each role can perform on resources within a group
const GROUP_ROLE_CAPABILITIES = {
  read: {
    sub_agents: new Set(['read']),
  },
  write: {
    sub_agents: new Set(['read', 'write']),
  },
  manager: {
    sub_agents: new Set(['read', 'write']),
    members: new Set(['add', 'remove', 'update_role']),
  },
} as const;

// All possible permissions to display
const PERMISSION_DEFINITIONS = {
  groups: {
    label: 'Groups (System)',
    actions: ['read', 'write', 'create'] as const,
    descriptions: {
      read: 'View groups you are a member of',
      write: 'Manage group details and settings',
      create: 'Create new groups',
    },
    source: 'system' as const,
  },
  users: {
    label: 'Users (System)',
    actions: ['read', 'write'] as const,
    descriptions: {
      read: 'View user information',
      write: 'Manage user accounts and roles',
    },
    source: 'system' as const,
  },
  sub_agents: {
    label: 'Sub-Agents',
    actions: ['read', 'write', 'approve'] as const,
    descriptions: {
      read: 'View sub-agents in your groups',
      write: 'Create and edit sub-agents',
      approve: 'Approve sub-agents you own OR sub-agents in groups where you have write/manager role (requires approver/admin system role)',
    },
    source: 'combined' as const, // Requires both system and group roles
  },
  members: {
    label: 'Group Members',
    actions: ['add', 'remove', 'update_role'] as const,
    descriptions: {
      add: 'Add members to groups you manage',
      remove: 'Remove members from groups you manage',
      update_role: 'Change member roles in groups you manage',
    },
    source: 'group' as const,
  },
} as const;

type SystemRole = keyof typeof SYSTEM_ROLE_CAPABILITIES;
type GroupRole = keyof typeof GROUP_ROLE_CAPABILITIES;

export function UserPermissionsTable() {
  const { user, isAdmin } = useAuth();

  const systemRole = (user?.role ?? 'member') as SystemRole;
  const userGroups = user?.groups ?? [];

  // Check if user has a system-level capability
  const hasSystemCapability = (resource: string, action: string): boolean => {
    const roleCapabilities = SYSTEM_ROLE_CAPABILITIES[systemRole];
    return roleCapabilities[resource as keyof typeof roleCapabilities]?.has(action) ?? false;
  };

  // Check if user has a group-level capability through any of their groups
  const hasGroupCapability = (resource: string, action: string): { has: boolean; groups: string[] } => {
    const grantingGroups: string[] = [];
    
    for (const group of userGroups) {
      const groupRole = group.group_role as GroupRole;
      const roleCapabilities = GROUP_ROLE_CAPABILITIES[groupRole];
      
      if (roleCapabilities[resource as keyof typeof roleCapabilities]?.has(action)) {
        grantingGroups.push(group.name);
      }
    }
    
    return { has: grantingGroups.length > 0, groups: grantingGroups };
  };

  // Check combined permissions (like approve)
  const hasPermission = (resource: string, action: string, source: 'system' | 'group' | 'combined'): { has: boolean; reason: string; groups?: string[] } => {
    if (isAdmin) {
      return { has: true, reason: 'System Administrator' };
    }

    if (source === 'system') {
      const has = hasSystemCapability(resource, action);
      return { has, reason: has ? `System role: ${systemRole}` : 'No system permission' };
    }

    if (source === 'group') {
      const groupCheck = hasGroupCapability(resource, action);
      return { 
        has: groupCheck.has, 
        reason: groupCheck.has ? 'Group role' : 'No group permission',
        groups: groupCheck.groups 
      };
    }

    // Combined: requires both system and group permissions
    if (action === 'approve') {
      const hasSystemApprove = hasSystemCapability(resource, action);
      const groupCheck = hasGroupCapability(resource, 'write'); // Need write access in group
      
      // Approvers/admins can approve resources they own, OR resources in groups with write access
      const canApproveOwned = hasSystemApprove;
      const canApproveInGroups = hasSystemApprove && groupCheck.has;
      
      const has = canApproveOwned || canApproveInGroups;
      let reason = '';
      
      if (!hasSystemApprove) {
        reason = 'Requires system approver or admin role';
      } else if (canApproveInGroups) {
        reason = `Granted for resources you own + resources in groups with write access`;
      } else {
        reason = 'Granted for resources you own';
      }
      
      return { has, reason, groups: groupCheck.groups };
    }

    // Default for other combined permissions
    const groupCheck = hasGroupCapability(resource, action);
    return { 
      has: groupCheck.has, 
      reason: groupCheck.has ? 'Group role' : 'No group permission',
      groups: groupCheck.groups 
    };
  };

  return (
    <div className="space-y-4">
      {/* System Role Info */}
      <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg border">
        <Shield className="h-4 w-4" />
        <div className="flex-1">
          <p className="text-sm font-medium">
            System Role: <Badge variant="outline">{systemRole}</Badge>
          </p>
          <p className="text-xs text-muted-foreground">
            {isAdmin ? 'You have full administrative access to all features' : 'Your system-wide capabilities'}
          </p>
        </div>
      </div>

      {/* Permission Model Explanation */}
      <div className="text-sm text-muted-foreground space-y-2 p-3 bg-muted/30 rounded-lg border">
        <p className="font-medium">Two-Level Permission System:</p>
        <ul className="list-disc list-inside space-y-1 text-xs">
          <li><strong>System Role</strong> ({systemRole}): Defines what actions you can perform system-wide</li>
          <li><strong>Group Roles</strong> (read/write/manager): Define which resources you can access within each group</li>
          <li><strong>Resource Ownership</strong>: You have full access (read/write/approve) to resources you own, regardless of group memberships</li>
          <li>Some actions have multiple paths (e.g., approval works for owned resources OR for resources in groups with write access)</li>
        </ul>
      </div>

      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Resource</TableHead>
              <TableHead>Permission</TableHead>
              <TableHead>Description</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Granted By</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(PERMISSION_DEFINITIONS).map(([resource, config]) => {
              return config.actions.map((action, index) => {
                const permCheck = hasPermission(resource, action, config.source);

                return (
                  <TableRow key={`${resource}-${action}`}>
                    {index === 0 && (
                      <TableCell rowSpan={config.actions.length} className="font-medium">
                        {config.label}
                      </TableCell>
                    )}
                    <TableCell>
                      <code className="text-xs bg-muted px-1.5 py-0.5 rounded">
                        {resource}.{action}
                      </code>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {config.descriptions[action as keyof typeof config.descriptions]}
                    </TableCell>
                    <TableCell>
                      {permCheck.has ? (
                        <Badge variant="default" className="gap-1">
                          <Check className="h-3 w-3" />
                          Granted
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="gap-1">
                          <X className="h-3 w-3" />
                          Not Granted
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <span className="text-xs text-muted-foreground">{permCheck.reason}</span>
                        {permCheck.groups && permCheck.groups.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {permCheck.groups.map((groupName: string) => (
                              <Badge key={groupName} variant="outline" className="text-xs">
                                {groupName}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              });
            })}
          </TableBody>
        </Table>
      </div>

      {!isAdmin && userGroups.length === 0 && (
        <div className="text-center py-4 text-sm text-muted-foreground">
          You are not a member of any groups. Without group membership, you can only access resources you own. Contact an administrator to get access to shared resources.
        </div>
      )}

      {userGroups.length > 0 && (
        <div className="text-sm">
          <p className="font-medium mb-2">Your Group Memberships:</p>
          <div className="space-y-2">
            {userGroups.map((group) => (
              <div key={group.id} className="flex items-center gap-2 p-2 bg-muted/30 rounded border">
                <Badge variant="outline">{group.name}</Badge>
                <span className="text-xs text-muted-foreground">•</span>
                <Badge variant="secondary" className="text-xs">
                  Role: {group.group_role || 'read'}
                </Badge>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
