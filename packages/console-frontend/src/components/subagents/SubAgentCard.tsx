import { useState } from 'react';
import { Bot, Globe, Terminal, Users, Database, User, Shield, UsersRound } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { SubAgentPermissionsDialog } from './SubAgentPermissionsDialog';
import { useAuth } from '@/contexts/AuthContext';
import type { SubAgent, SubAgentStatus } from './types';

const statusConfig: Record<SubAgentStatus, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  draft: { label: 'Draft', variant: 'secondary' },
  pending_approval: { label: 'Pending Approval', variant: 'outline' },
  approved: { label: 'Approved', variant: 'default' },
  rejected: { label: 'Rejected', variant: 'destructive' },
};

interface SubAgentCardProps {
  subAgent: SubAgent;
  onClick?: () => void;
  showOwner?: boolean;
  showManageAccess?: boolean;
}

export function SubAgentCard({ subAgent, onClick, showOwner = true, showManageAccess = false }: SubAgentCardProps) {
  const { user, adminMode } = useAuth();
  const [showPermissionsDialog, setShowPermissionsDialog] = useState(false);
  
  // Get status from embedded config_version
  const status = subAgent.config_version?.status ?? 'draft';
  const statusInfo = statusConfig[status];
  const TypeIcon = subAgent.type === 'remote' ? Globe : subAgent.type === 'foundry' ? Database : Terminal;
  const isAutomated = subAgent.type === 'automated';
  
  const isOwner = subAgent.owner_user_id === user?.id;
  const isAdministrator = user?.is_administrator ?? false;
  const canManageAccess = showManageAccess && (isOwner || (isAdministrator && adminMode));

  // Version info
  const defaultVersion = subAgent.default_version;
  const currentVersion = subAgent.current_version ?? 1;
  const configVersion = subAgent.config_version;
  
  // Helper to format version label
  const formatVersionLabel = (versionNum: number | null | undefined, isDefaultVersion: boolean): string => {
    if (versionNum == null) return '';
    // For the default (live) version, use release_number if available
    if (isDefaultVersion && configVersion?.release_number) {
      return `v${configVersion.release_number}`;
    }
    // For current version, check if it has hash (draft/pending) or release_number (approved)
    if (!isDefaultVersion && configVersion) {
      if (configVersion.status === 'approved' && configVersion.release_number) {
        return `v${configVersion.release_number}`;
      }
      if (configVersion.version_hash) {
        return `#${configVersion.version_hash.slice(0, 7)}`;
      }
    }
    return `v${versionNum}`;
  };
  
  // Show different badge if we're on a draft version of an otherwise approved agent
  const isApproved = defaultVersion !== null && defaultVersion !== undefined;

  const handleManageAccessClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    setShowPermissionsDialog(true);
  };

  return (
    <>
      <div
        className="group flex flex-col gap-3 rounded-lg border bg-card p-4 transition-colors hover:bg-accent/50 cursor-pointer"
        onClick={onClick}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            onClick?.();
          }
        }}
      >
        {/* Header with title and main status */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-start gap-2 min-w-0">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10">
              <Bot className="h-4 w-4 text-primary" />
            </div>
            <div className="min-w-0">
              <h3 className="font-medium leading-none truncate">{subAgent.name}</h3>
              {showOwner && subAgent.owner && (
                <p className="mt-1 text-xs text-muted-foreground">by {subAgent.owner.name}</p>
              )}
            </div>
          </div>
          <div className="flex flex-col items-end gap-1">
            {/* Show production status if has default version */}
            {isApproved && (
              <Badge variant="default" className="shrink-0">
                {formatVersionLabel(defaultVersion, true)} Live
              </Badge>
            )}
            {/* Show current version status if different from default */}
            {isApproved && currentVersion !== defaultVersion && (
              <Badge variant={statusInfo.variant} className="shrink-0">
                {formatVersionLabel(currentVersion, false)} {statusInfo.label}
              </Badge>
            )}
            {/* If no default version, just show current status */}
            {!isApproved && (
              <Badge variant={statusInfo.variant} className="shrink-0">
                {statusInfo.label}
              </Badge>
            )}
          </div>
        </div>

        {subAgent.config_version?.description && (
          <p className="text-sm text-muted-foreground line-clamp-2">{subAgent.config_version.description}</p>
        )}

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <TypeIcon className="h-3 w-3" />
              <span className="capitalize">{subAgent.type}</span>
            </span>
            {isAutomated && (
              <Badge variant="secondary" className="flex items-center gap-1 text-xs">
                🤖 Automated
              </Badge>
            )}
            {subAgent.activated_by && (
              <>
                {subAgent.activated_by === 'user' && (
                  <Badge variant="outline" className="flex items-center gap-1 text-xs">
                    <User className="h-3 w-3" />
                    Self-enabled
                  </Badge>
                )}
                {subAgent.activated_by === 'group' && (
                  <Badge variant="secondary" className="flex items-center gap-1 text-xs">
                    <UsersRound className="h-3 w-3" />
                    Group default
                    {subAgent.activated_by_groups && subAgent.activated_by_groups.length > 0 && (
                      <span className="ml-1">({subAgent.activated_by_groups.length})</span>
                    )}
                  </Badge>
                )}
                {subAgent.activated_by === 'admin' && (
                  <Badge variant="default" className="flex items-center gap-1 text-xs">
                    <Shield className="h-3 w-3" />
                    Admin-enabled
                  </Badge>
                )}
              </>
            )}
          </div>
          <div className="flex items-center gap-1">
            {canManageAccess && (
              <Button
                variant="ghost"
                size="sm"
                className="opacity-0 group-hover:opacity-100 transition-opacity"
                onClick={handleManageAccessClick}
                title="Manage group access"
              >
                <Users className="h-4 w-4" />
              </Button>
            )}
            <Button variant="ghost" size="sm" className="opacity-0 group-hover:opacity-100 transition-opacity">
              Open
            </Button>
          </div>
        </div>

        {status === 'rejected' && subAgent.config_version?.rejection_reason && (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <strong>Rejection reason:</strong> {subAgent.config_version.rejection_reason}
          </div>
        )}
      </div>

      {canManageAccess && (
        <SubAgentPermissionsDialog
          subAgentId={subAgent.id}
          subAgentName={subAgent.name}
          open={showPermissionsDialog}
          onOpenChange={setShowPermissionsDialog}
        />
      )}
    </>
  );
}
