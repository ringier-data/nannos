import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ShieldOff, Trash2, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { client } from '@/api/generated/client.gen';

interface BypassRule {
  bypass_all?: boolean;
  bypass_patterns?: Record<string, string[]>;
}

type BypassRules = Record<string, BypassRule>;

/** Fetch bypass rules from user settings. */
async function fetchBypassRules(): Promise<BypassRules> {
  const res = await client.get({ url: '/api/v1/auth/me/settings' });
  const wrapper = res.data as { data?: Record<string, unknown> } | undefined;
  const data = wrapper?.data;
  return (data?.tool_bypass_rules as BypassRules) ?? {};
}

/** Remove a single bypass rule. */
async function removeBypassRule(key: string): Promise<void> {
  const [toolName, serverSlug] = key.split('::');
  await client.put({
    url: '/api/v1/auth/me/settings/tool-bypass',
    body: { tool_name: toolName, server_slug: serverSlug, remove: true },
  });
}

export function ToolBypassRulesList() {
  const queryClient = useQueryClient();
  const [removingKey, setRemovingKey] = useState<string | null>(null);
  const [pendingRemoveKey, setPendingRemoveKey] = useState<string | null>(null);

  const { data: rules, isLoading } = useQuery({
    queryKey: ['toolBypassRules'],
    queryFn: fetchBypassRules,
  });

  const removeMutation = useMutation({
    mutationFn: removeBypassRule,
    onSuccess: () => {
      toast.success('Bypass rule removed');
      queryClient.invalidateQueries({ queryKey: ['toolBypassRules'] });
      queryClient.invalidateQueries({ queryKey: ['getCurrentUserSettingsApiV1AuthMeSettingsGet'] });
      setRemovingKey(null);
    },
    onError: () => {
      toast.error('Failed to remove bypass rule');
      setRemovingKey(null);
    },
  });

  const handleRemove = (key: string) => {
    setRemovingKey(key);
    removeMutation.mutate(key);
  };

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm py-4">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading bypass rules...
      </div>
    );
  }

  const entries = Object.entries(rules ?? {});

  if (entries.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-4 flex items-center gap-2">
        <ShieldOff className="h-4 w-4" />
        No bypass rules configured. Tools will prompt for approval based on their risk score.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {entries.map(([key, rule]) => {
        const [toolName, serverSlug] = key.split('::');
        const isRemoving = removingKey === key;

        return (
          <div
            key={key}
            className="flex items-center justify-between gap-3 rounded-md border px-3 py-2"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium truncate">{toolName}</p>
              <p className="text-xs text-muted-foreground">
                {serverSlug !== '_self' && <span>Server: {serverSlug} · </span>}
                {rule.bypass_all ? (
                  <span>Always allowed</span>
                ) : rule.bypass_patterns ? (
                  <span>
                    Patterns:{' '}
                    {Object.entries(rule.bypass_patterns)
                      .map(([param, patterns]) => `${param}: ${patterns.join(', ')}`)
                      .join('; ')}
                  </span>
                ) : (
                  <span>Bypass active</span>
                )}
              </p>
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-destructive hover:text-destructive"
                  onClick={() => setPendingRemoveKey(key)}
                  disabled={isRemoving}
                >
                  {isRemoving ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Trash2 className="h-4 w-4" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>Remove bypass rule (will ask for approval again)</TooltipContent>
            </Tooltip>
          </div>
        );
      })}

      <ConfirmDialog
        open={pendingRemoveKey !== null}
        onOpenChange={(o) => { if (!o) setPendingRemoveKey(null); }}
        title="Remove bypass rule?"
        description="This tool will prompt for approval again based on its risk score."
        confirmLabel="Remove"
        variant="destructive"
        isLoading={removeMutation.isPending}
        onConfirm={() => {
          if (pendingRemoveKey) handleRemove(pendingRemoveKey);
          setPendingRemoveKey(null);
        }}
      />
    </div>
  );
}
