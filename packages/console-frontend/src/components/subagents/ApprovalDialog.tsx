import { useState, useMemo } from 'react';
import { CheckCircle, XCircle, GitCompare, ChevronDown, ChevronUp, Info } from 'lucide-react';
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { SubAgent, SubAgentConfigVersion } from './types';

interface ApprovalDialogProps {
  subAgent: SubAgent;
  action: 'approve' | 'reject';
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (action: 'approve' | 'reject', rejectionReason?: string) => Promise<void>;
  /** Version being reviewed (for version-level approval) */
  version?: SubAgentConfigVersion;
  /** Previous/default version to compare against */
  compareVersion?: SubAgentConfigVersion;
}

/**
 * Format version data for diff display.
 * Includes description, model, system_prompt/agent_url, and mcp_tools with proper multi-line rendering.
 */
function formatVersionForDiff(version: SubAgentConfigVersion | null | undefined): string {
  if (!version) return '';
  
  const sections: string[] = [];
  
  // Description section - crucial for orchestrator routing
  sections.push('=== DESCRIPTION (Agent Skills) ===');
  sections.push('# The orchestrator uses this to decide when to delegate tasks to this agent.');
  sections.push('');
  if (version.description) {
    sections.push(version.description);
  } else {
    sections.push('(no description)');
  }
  sections.push('');
  
  // Model section
  sections.push('=== MODEL ===');
  sections.push(version.model || '(default)');
  sections.push(version.enable_thinking ? `Enable Thinking: true` : `Enable Thinking: false`);
  if (version.enable_thinking){
    sections.push(`Thinking Level: ${version.thinking_level || '(default)'}`);
  }
  sections.push('');
  
  // Configuration section - different for local vs remote agents
  sections.push('=== CONFIGURATION ===');
  
  if (version.system_prompt) {
    // Local agent configuration
    sections.push('Type: Local Agent');
    sections.push('');
    sections.push('system_prompt:');
    sections.push('"""');
    sections.push(version.system_prompt);
    sections.push('"""');
    sections.push('');
  } else if (version.agent_url) {
    // Remote agent configuration
    sections.push('Type: Remote Agent');
    sections.push('');
    sections.push(`agent_url: ${version.agent_url}`);
    sections.push('');
  }
  
  // MCP Tools section
  if (version.mcp_tools && version.mcp_tools.length > 0) {
    sections.push('=== MCP TOOLS ===');
    version.mcp_tools.forEach(tool => {
      sections.push(`- ${tool}`);
    });
    sections.push('');
  }
  
  return sections.join('\n');
}

export function ApprovalDialog({
  subAgent,
  action,
  open,
  onOpenChange,
  onConfirm,
  version,
  compareVersion,
}: ApprovalDialogProps) {
  const [rejectionReason, setRejectionReason] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showDiff, setShowDiff] = useState(false);

  const isVersionApproval = !!version;
  const hasChanges = isVersionApproval && compareVersion;

  const { oldValue, newValue } = useMemo(() => {
    return {
      oldValue: formatVersionForDiff(compareVersion),
      newValue: formatVersionForDiff(version),
    };
  }, [version, compareVersion]);

  const handleConfirm = async () => {
    if (action === 'reject' && !rejectionReason.trim()) {
      toast.error('Validation Error', { description: 'Please provide a reason for rejection' });
      return;
    }

    setIsSubmitting(true);

    try {
      await onConfirm(action, action === 'reject' ? rejectionReason.trim() : undefined);
      onOpenChange(false);
      setRejectionReason('');
      setShowDiff(false);
    } catch (err) {
      toast.error('Error', { description: err instanceof Error ? err.message : 'An error occurred' });
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleOpenChange = (newOpen: boolean) => {
    if (!newOpen) {
      setRejectionReason('');
      setShowDiff(false);
    }
    onOpenChange(newOpen);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className={hasChanges ? 'max-w-4xl' : undefined}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {action === 'approve' ? (
              <>
                <CheckCircle className="h-5 w-5 text-green-600" />
                {isVersionApproval ? 'Approve Version' : 'Approve Sub-Agent'}
              </>
            ) : (
              <>
                <XCircle className="h-5 w-5 text-destructive" />
                {isVersionApproval ? 'Reject Version' : 'Reject Sub-Agent'}
              </>
            )}
          </DialogTitle>
          <DialogDescription>
            {action === 'approve'
              ? isVersionApproval
                ? `Review and approve version ${version?.version} of "${subAgent.name}".`
                : `Are you sure you want to approve "${subAgent.name}"? Once approved, it will be available for use.`
              : isVersionApproval
                ? `Please provide a reason for rejecting version ${version?.version} of "${subAgent.name}".`
                : `Please provide a reason for rejecting "${subAgent.name}".`}
          </DialogDescription>
        </DialogHeader>

        {isVersionApproval && (
          <div className="flex items-center gap-2 py-2">
            <Badge variant="outline">Version {version?.version}</Badge>
            {version?.change_summary && (
              <span className="text-sm text-muted-foreground">{version.change_summary}</span>
            )}
          </div>
        )}

        {hasChanges && (
          <Collapsible open={showDiff} onOpenChange={setShowDiff}>
            <CollapsibleTrigger asChild>
              <Button variant="outline" className="w-full justify-between">
                <span className="flex items-center gap-2">
                  <GitCompare className="h-4 w-4" />
                  View changes (description, model, system prompt, MCP tools)
                </span>
                {showDiff ? (
                  <ChevronUp className="h-4 w-4" />
                ) : (
                  <ChevronDown className="h-4 w-4" />
                )}
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-2">
              <Alert className="mb-2 border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/50">
                <Info className="h-4 w-4 text-blue-600" />
                <AlertDescription className="text-blue-700 dark:text-blue-400">
                  <strong>Review the Description section carefully.</strong> It defines the agent's skill set and the orchestrator 
                  uses it to determine when to delegate tasks.
                </AlertDescription>
              </Alert>
              <div className="rounded-md border overflow-hidden max-h-[300px] overflow-y-auto">
                <ReactDiffViewer
                  oldValue={oldValue}
                  newValue={newValue}
                  splitView={true}
                  compareMethod={DiffMethod.LINES}
                  leftTitle={`Version ${compareVersion?.version ?? '?'} (current)`}
                  rightTitle={`Version ${version?.version ?? '?'} (pending)`}
                  styles={{
                    variables: {
                      dark: {
                        diffViewerBackground: 'hsl(var(--card))',
                        diffViewerColor: 'hsl(var(--card-foreground))',
                        addedBackground: 'hsl(142.1 76.2% 36.3% / 0.2)',
                        addedColor: 'hsl(142.1 70.6% 45.3%)',
                        removedBackground: 'hsl(0 84.2% 60.2% / 0.2)',
                        removedColor: 'hsl(0 84.2% 60.2%)',
                        wordAddedBackground: 'hsl(142.1 76.2% 36.3% / 0.4)',
                        wordRemovedBackground: 'hsl(0 84.2% 60.2% / 0.4)',
                        addedGutterBackground: 'hsl(142.1 76.2% 36.3% / 0.1)',
                        removedGutterBackground: 'hsl(0 84.2% 60.2% / 0.1)',
                        gutterBackground: 'hsl(var(--muted))',
                        gutterBackgroundDark: 'hsl(var(--muted))',
                        highlightBackground: 'hsl(var(--accent))',
                        highlightGutterBackground: 'hsl(var(--accent))',
                        codeFoldGutterBackground: 'hsl(var(--muted))',
                        codeFoldBackground: 'hsl(var(--muted))',
                        emptyLineBackground: 'hsl(var(--muted))',
                        codeFoldContentColor: 'hsl(var(--muted-foreground))',
                      },
                      light: {
                        diffViewerBackground: 'hsl(var(--card))',
                        diffViewerColor: 'hsl(var(--card-foreground))',
                        addedBackground: 'hsl(142.1 76.2% 36.3% / 0.1)',
                        addedColor: 'hsl(142.1 70.6% 45.3%)',
                        removedBackground: 'hsl(0 84.2% 60.2% / 0.1)',
                        removedColor: 'hsl(0 84.2% 60.2%)',
                        wordAddedBackground: 'hsl(142.1 76.2% 36.3% / 0.3)',
                        wordRemovedBackground: 'hsl(0 84.2% 60.2% / 0.3)',
                        addedGutterBackground: 'hsl(142.1 76.2% 36.3% / 0.05)',
                        removedGutterBackground: 'hsl(0 84.2% 60.2% / 0.05)',
                        gutterBackground: 'hsl(var(--muted))',
                        gutterBackgroundDark: 'hsl(var(--muted))',
                        highlightBackground: 'hsl(var(--accent))',
                        highlightGutterBackground: 'hsl(var(--accent))',
                        codeFoldGutterBackground: 'hsl(var(--muted))',
                        codeFoldBackground: 'hsl(var(--muted))',
                        emptyLineBackground: 'hsl(var(--muted))',
                        codeFoldContentColor: 'hsl(var(--muted-foreground))',
                      },
                    },
                    contentText: {
                      fontFamily: 'ui-monospace, monospace',
                      fontSize: '12px',
                    },
                  }}
                  useDarkTheme={document.documentElement.classList.contains('dark')}
                />
              </div>
            </CollapsibleContent>
          </Collapsible>
        )}

        {action === 'reject' && (
          <div className="space-y-2 py-4">
            <Label htmlFor="rejectionReason">Rejection Reason *</Label>
            <Textarea
              id="rejectionReason"
              value={rejectionReason}
              onChange={(e) => setRejectionReason(e.target.value)}
              placeholder="Please explain why this sub-agent is being rejected..."
              rows={4}
              disabled={isSubmitting}
            />
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button
            variant={action === 'approve' ? 'default' : 'destructive'}
            onClick={handleConfirm}
            disabled={isSubmitting}
          >
            {isSubmitting ? 'Processing...' : action === 'approve' ? 'Approve' : 'Reject'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
