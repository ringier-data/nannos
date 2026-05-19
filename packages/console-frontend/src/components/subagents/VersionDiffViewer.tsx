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
  
  const lines: string[] = [];

  // --- description ---
  lines.push('diff --config a/description b/description');
  lines.push('--- a/description');
  lines.push('+++ b/description');
  lines.push(version.description || '(no description)');
  lines.push('');

  // --- configuration ---
  if (version.system_prompt) {
    lines.push('diff --config a/config b/config');
    lines.push('--- a/config');
    lines.push('+++ b/config');
    lines.push(`type: local`);
    lines.push(`model: ${version.model || '(default)'}`);
    lines.push(`enable_thinking: ${version.enable_thinking ?? false}`);
    if (version.enable_thinking) {
      lines.push(`thinking_level: ${version.thinking_level || '(default)'}`);
    }
    lines.push(`sandbox_enabled: ${version.sandbox_enabled ?? false}`);
    lines.push('');

    lines.push('diff --config a/system_prompt b/system_prompt');
    lines.push('--- a/system_prompt');
    lines.push('+++ b/system_prompt');
    lines.push(version.system_prompt);
    lines.push('');
  } else if (version.agent_url) {
    lines.push('diff --config a/config b/config');
    lines.push('--- a/config');
    lines.push('+++ b/config');
    lines.push(`type: remote`);
    lines.push(`agent_url: ${version.agent_url}`);
    lines.push(`sandbox_enabled: ${version.sandbox_enabled ?? false}`);
    lines.push('');
  } else if (version.foundry_hostname) {
    lines.push('diff --config a/config b/config');
    lines.push('--- a/config');
    lines.push('+++ b/config');
    lines.push(`type: foundry`);
    lines.push(`foundry_hostname: ${version.foundry_hostname}`);
    lines.push(`foundry_client_id: ${version.foundry_client_id || '(not set)'}`);
    lines.push(`foundry_client_secret_ref: ${version.foundry_client_secret_ref || '(not set)'}`);
    lines.push(`foundry_ontology_rid: ${version.foundry_ontology_rid || '(not set)'}`);
    lines.push(`foundry_query_api_name: ${version.foundry_query_api_name || '(not set)'}`);
    if (version.foundry_scopes && version.foundry_scopes.length > 0) {
      lines.push(`foundry_scopes: ${version.foundry_scopes.join(', ')}`);
    }
    if (version.foundry_version) {
      lines.push(`foundry_version: ${version.foundry_version}`);
    }
    lines.push(`sandbox_enabled: ${version.sandbox_enabled ?? false}`);
    lines.push('');
  }

  // --- mcp_tools ---
  if (version.mcp_tools && version.mcp_tools.length > 0) {
    lines.push('diff --config a/mcp_tools b/mcp_tools');
    lines.push('--- a/mcp_tools');
    lines.push('+++ b/mcp_tools');
    version.mcp_tools.forEach(tool => {
      lines.push(tool);
    });
    lines.push('');
  }

  // --- skills ---
  if (version.skills && version.skills.length > 0) {
    version.skills.forEach(skill => {
      const skillPath = `skills/${skill.name}`;
      lines.push(`diff --config a/${skillPath}/SKILL.md b/${skillPath}/SKILL.md`);
      lines.push(`--- a/${skillPath}/SKILL.md`);
      lines.push(`+++ b/${skillPath}/SKILL.md`);
      lines.push(`# ${skill.name}`);
      if (skill.description) {
        lines.push(`description: ${skill.description}`);
      }
      if ((skill as any).source) {
        lines.push(`source: ${(skill as any).source}`);
      }
      if (skill.body) {
        lines.push('');
        lines.push(skill.body);
      }
      lines.push('');

      if (skill.files && skill.files.length > 0) {
        // Show file manifest with sizes instead of full contents to keep diffs fast
        lines.push(`diff --config a/${skillPath}/files b/${skillPath}/files`);
        lines.push(`--- a/${skillPath}/files`);
        lines.push(`+++ b/${skillPath}/files`);
        lines.push(`# ${skill.files.length} file(s)`);
        skill.files.forEach(f => {
          const sizeKb = f.content ? (f.content.length / 1024).toFixed(1) : '0.0';
          lines.push(`  ${f.path} (${sizeKb} KB)`);
        });
        lines.push('');
      }
    });
  }

  return lines.join('\n');
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
