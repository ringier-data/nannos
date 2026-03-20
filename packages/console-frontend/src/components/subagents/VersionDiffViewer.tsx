import { useMemo } from 'react';
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import { Loader2, Info } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Alert, AlertDescription } from '@/components/ui/alert';
import type { SubAgentConfigVersion } from './types';

interface VersionDiffViewerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fromVersion: SubAgentConfigVersion | null;
  toVersion: SubAgentConfigVersion | null;
  subAgentName: string;
  isLoading?: boolean;
}

/**
 * Format version data for diff display.
 * Includes description, model, system_prompt/agent_url, and mcp_tools with proper multi-line rendering.
 */
function formatVersionForDiff(version: SubAgentConfigVersion | null): string {
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
  
  // Configuration section - different for local vs remote vs Foundry agents
  sections.push('=== CONFIGURATION ===');
  
  if (version.system_prompt) {
    // Local agent configuration
    sections.push('Type: Local Agent');
    sections.push('');
    sections.push(`model: ${version.model || '(default)'}`);
    if (version.enable_thinking !== undefined) {
      sections.push(`enable_thinking: ${version.enable_thinking}`);
    }
    if (version.enable_thinking) {
      sections.push(`thinking_level: ${version.thinking_level || '(default)'}`);
    }
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
  } else if (version.foundry_hostname) {
    // Foundry agent configuration
    sections.push('Type: Foundry Agent');
    sections.push('');
    sections.push(`foundry_hostname: ${version.foundry_hostname}`);
    sections.push(`foundry_client_id: ${version.foundry_client_id || '(not set)'}`);
    sections.push(`foundry_client_secret_ref: ${version.foundry_client_secret_ref || '(not set)'}`);
    sections.push(`foundry_ontology_rid: ${version.foundry_ontology_rid || '(not set)'}`);
    sections.push(`foundry_query_api_name: ${version.foundry_query_api_name || '(not set)'}`);
    if (version.foundry_scopes && version.foundry_scopes.length > 0) {
      sections.push('foundry_scopes:');
      version.foundry_scopes.forEach(scope => {
        sections.push(`  - ${scope}`);
      });
    } else {
      sections.push('foundry_scopes: (none)');
    }
    if (version.foundry_version) {
      sections.push(`foundry_version: ${version.foundry_version}`);
    }
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

/**
 * Format version label for display (hash for draft/pending, release number for approved)
 */
function formatVersionLabel(version: SubAgentConfigVersion | null): string {
  if (!version) return '?';
  if (version.status === 'approved' && version.release_number) {
    return `v${version.release_number}`;
  }
  if (version.version_hash) {
    return `#${version.version_hash.slice(0, 7)}`;
  }
  return `v${version.version}`;
}

export function VersionDiffViewer({
  open,
  onOpenChange,
  fromVersion,
  toVersion,
  subAgentName,
  isLoading = false,
}: VersionDiffViewerProps) {
  const { oldValue, newValue } = useMemo(() => {
    return {
      oldValue: formatVersionForDiff(fromVersion),
      newValue: formatVersionForDiff(toVersion),
    };
  }, [fromVersion, toVersion]);

  const fromLabel = formatVersionLabel(fromVersion);
  const toLabel = formatVersionLabel(toVersion);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="!w-[98vw] !max-w-[1600px] max-h-[90vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Configuration Diff - {subAgentName}</DialogTitle>
          <DialogDescription>
            Comparing {fromLabel} → {toLabel}
          </DialogDescription>
        </DialogHeader>

        <Alert className="border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/50">
          <Info className="h-4 w-4 text-blue-600" />
          <AlertDescription className="text-blue-700 dark:text-blue-400">
            <strong>Review the Description section carefully.</strong> It defines the agent's skill set and the orchestrator 
            uses it to determine when to delegate tasks to this agent.
          </AlertDescription>
        </Alert>

        <div className="flex-1 overflow-auto rounded-md border">
          {isLoading ? (
            <div className="flex items-center justify-center h-48">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <ReactDiffViewer
              oldValue={oldValue}
              newValue={newValue}
              splitView={true}
              compareMethod={DiffMethod.LINES}
              leftTitle={fromLabel}
              rightTitle={toLabel}
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
                  fontSize: '13px',
                },
              }}
              useDarkTheme={document.documentElement.classList.contains('dark')}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
