import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { UserCheck, UserX, Trash2, Loader2, ShieldQuestion } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { ConfirmDialog } from '@/components/admin/ConfirmDialog';
import { client } from '@/api/generated/client.gen';

/**
 * Remembered identity-disclosure answers, keyed by MCP server slug: approving one
 * lets every identity-scoped tool of that integration receive the user's verified
 * email address so it can scope access to their own records (ADR 0006).
 */
interface ConsentGrant {
  granted?: boolean;
}

type ConsentGrants = Record<string, ConsentGrant>;

async function fetchConsentGrants(): Promise<ConsentGrants> {
  const res = await client.get({ url: '/api/v1/auth/me/settings' });
  const wrapper = res.data as { data?: Record<string, unknown> } | undefined;
  return (wrapper?.data?.identity_consent_grants as ConsentGrants) ?? {};
}

/** Set an answer, or forget it (`remove`) so the agent asks again on next use. */
async function updateConsent(vars: { serverSlug: string; granted?: boolean; remove?: boolean }): Promise<void> {
  await client.put({
    url: '/api/v1/auth/me/settings/identity-consent',
    body: vars.remove
      ? { server_slug: vars.serverSlug, remove: true }
      : { server_slug: vars.serverSlug, granted: vars.granted },
  });
}

export function IdentityConsentList() {
  const queryClient = useQueryClient();
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [pendingRemoveSlug, setPendingRemoveSlug] = useState<string | null>(null);

  const { data: grants, isLoading } = useQuery({
    queryKey: ['identityConsentGrants'],
    queryFn: fetchConsentGrants,
  });

  const mutation = useMutation({
    mutationFn: updateConsent,
    onSuccess: (_data, vars) => {
      toast.success(
        vars.remove
          ? 'Answer forgotten — you will be asked again'
          : vars.granted
            ? 'Identity sharing allowed'
            : 'Identity sharing blocked',
      );
      queryClient.invalidateQueries({ queryKey: ['identityConsentGrants'] });
      queryClient.invalidateQueries({ queryKey: ['getCurrentUserSettingsApiV1AuthMeSettingsGet'] });
      setBusySlug(null);
    },
    onError: () => {
      toast.error('Failed to update identity sharing');
      setBusySlug(null);
    },
  });

  const run = (vars: { serverSlug: string; granted?: boolean; remove?: boolean }) => {
    setBusySlug(vars.serverSlug);
    mutation.mutate(vars);
  };

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm py-4">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading identity sharing...
      </div>
    );
  }

  const entries = Object.entries(grants ?? {});

  if (entries.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-4 flex items-center gap-2">
        <ShieldQuestion className="h-4 w-4" />
        No integrations answered yet. The first time one needs your identity, you&apos;ll be asked.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {entries.map(([serverSlug, grant]) => {
        const granted = grant?.granted === true;
        const isBusy = busySlug === serverSlug;

        return (
          <div key={serverSlug} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium truncate">{serverSlug}</p>
              <p className="text-xs text-muted-foreground">
                {granted ? (
                  <span className="text-emerald-600 dark:text-emerald-400">
                    Receives your verified email address
                  </span>
                ) : (
                  <span className="text-destructive">
                    Blocked — its identity-scoped tools cannot run
                  </span>
                )}
              </p>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    onClick={() => run({ serverSlug, granted: !granted })}
                    disabled={isBusy}
                  >
                    {isBusy ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : granted ? (
                      <UserX className="h-4 w-4 text-destructive" />
                    ) : (
                      <UserCheck className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {granted
                    ? 'Block this integration from receiving your email'
                    : 'Allow this integration to receive your email'}
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive hover:text-destructive"
                    onClick={() => setPendingRemoveSlug(serverSlug)}
                    disabled={isBusy}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Forget this answer (you&apos;ll be asked again)</TooltipContent>
              </Tooltip>
            </div>
          </div>
        );
      })}

      <ConfirmDialog
        open={pendingRemoveSlug !== null}
        onOpenChange={(o) => {
          if (!o) setPendingRemoveSlug(null);
        }}
        title="Forget this answer?"
        description="The next time a tool from this integration needs your identity, you'll be asked again."
        confirmLabel="Forget"
        variant="destructive"
        isLoading={mutation.isPending}
        onConfirm={() => {
          if (pendingRemoveSlug) run({ serverSlug: pendingRemoveSlug, remove: true });
          setPendingRemoveSlug(null);
        }}
      />
    </div>
  );
}
