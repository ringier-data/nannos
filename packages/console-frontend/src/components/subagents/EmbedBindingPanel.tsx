import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { AlertCircle, ExternalLink, Loader2, Pencil, Plug, RefreshCw, Unlink } from 'lucide-react';
import { toast } from 'sonner';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { EmbedBindingDialog } from '@/components/subagents/EmbedBindingDialog';
import {
  refreshEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingRefreshPostMutation,
  removeEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingDeleteMutation,
  setEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingPutMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import type { EmbedBinding } from '@/api/generated/types.gen';
import { getErrorMessage } from '@/lib/utils';

interface EmbedBindingPanelProps {
  subAgentId: number;
  binding: EmbedBinding | null | undefined;
  /** Admin in admin mode: may bind, edit, refresh and remove. Everyone else sees a read-only summary. */
  canManage: boolean;
  onChanged: () => void;
}

function ago(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return formatDistanceToNow(date, { addSuffix: true });
}

/**
 * Host binding for a sub-agent whose definition is published by an embedding host (ADR-0006).
 *
 * The host serves `/.well-known/agent-skills/`; console-backend syncs it into approved
 * versions and activates every user whose token `azp` is listed here.
 */
export function EmbedBindingPanel({ subAgentId, binding, canManage, onChanged }: EmbedBindingPanelProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const openDialog = () => setDialogOpen(true);

  const setMutation = useMutation({
    ...setEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingPutMutation(),
    onSuccess: (result) => {
      setDialogOpen(false);
      onChanged();
      if (result.last_error) {
        toast.warning('Embedded agent saved, but the first fetch failed', { description: result.last_error });
      } else {
        toast.success('Embedded agent saved', { description: 'Published definition synced.' });
      }
    },
    onError: (err) => {
      toast.error('Failed to save embedded agent', { description: getErrorMessage(err) });
    },
  });

  const refreshMutation = useMutation({
    ...refreshEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingRefreshPostMutation(),
    onSuccess: (result) => {
      onChanged();
      if (result.last_error) {
        toast.error('Fetch failed', { description: result.last_error });
      } else {
        toast.success('Published definition fetched');
      }
    },
    onError: (err) => {
      toast.error('Failed to fetch the published definition', { description: getErrorMessage(err) });
    },
  });

  const removeMutation = useMutation({
    ...removeEmbedBindingApiV1SubAgentsSubAgentIdEmbedBindingDeleteMutation(),
    onSuccess: () => {
      setConfirmRemove(false);
      onChanged();
      toast.success('No longer embedded');
    },
    onError: (err) => {
      toast.error('Failed to stop embedding', { description: getErrorMessage(err) });
    },
  });

  if (!binding && !canManage) return null;

  const fetchedAgo = ago(binding?.fetched_at);
  const errorAgo = ago(binding?.last_error_at);

  return (
    <TooltipProvider>
      <div className="flex flex-col rounded-lg border border-border bg-muted/30 overflow-hidden flex-shrink-0">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border shrink-0">
          <Plug className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold flex-1">Embedded Nannos Assistant</h2>
          {binding && canManage && (
            <div className="flex items-center gap-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => refreshMutation.mutate({ path: { sub_agent_id: subAgentId } })}
                    disabled={refreshMutation.isPending}
                  >
                    {refreshMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <RefreshCw className="h-4 w-4" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Fetch the published definition now</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={openDialog}>
                    <Pencil className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Edit authority or client ids</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-destructive hover:text-destructive"
                    onClick={() => setConfirmRemove(true)}
                  >
                    <Unlink className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Stop embedding</TooltipContent>
              </Tooltip>
            </div>
          )}
        </div>

        <div className="p-4 space-y-3 text-sm">
          {!binding ? (
            <>
              <p className="text-muted-foreground">Not embedded. This sub-agent is defined here in Nannos.</p>
              <Button variant="outline" size="sm" className="w-full" onClick={openDialog}>
                <Plug className="mr-2 h-4 w-4" />
                Embed in an application
              </Button>
            </>
          ) : (
            <>
              <div className="text-muted-foreground mb-4">
                This agent's configuration is not managed here. If you want to update this sub-agent you must update the
                published definition in the linked application.
              </div>
              <dl className="text-xs [&>dt]:font-semibold [&>dt]:mt-3 [&>dt:first-child]:mt-0 [&>dd]:mt-0.5">
                <dt>Authority</dt>
                <dd className="min-w-0">
                  <a
                    href={binding.index_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-primary hover:underline inline-flex items-center gap-1 break-all"
                  >
                    {binding.index_url}
                    <ExternalLink className="h-3 w-3 shrink-0 opacity-60" />
                  </a>
                </dd>

                <dt>Clients</dt>
                <dd className="flex flex-wrap gap-1">
                  <ul>
                    {binding.azps.map((azp) => (
                      <li key={azp}>{azp}</li>
                    ))}
                  </ul>
                </dd>

                <dt>Last sync</dt>
                <dd className="min-w-0">
                  {fetchedAgo ? (
                    <span className="inline-flex items-center gap-1.5">{fetchedAgo}</span>
                  ) : (
                    <span className="text-muted-foreground">never</span>
                  )}
                </dd>

                {binding.agent && (
                  <>
                    <dt>Discovered Name</dt>
                    <dd className="min-w-0 space-y-0.5">
                      <div className="truncate" title={binding.agent.name}>
                        {binding.agent.name}
                        {binding.agent.organization && (
                          <span className="text-muted-foreground"> · {binding.agent.organization}</span>
                        )}
                      </div>
                    </dd>
                  </>
                )}
              </dl>

              {binding.last_error && (
                <Alert variant="destructive" className="py-2">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription className="text-xs break-words">
                    <span className="font-medium">Last fetch failed{errorAgo ? ` ${errorAgo}` : ''}.</span>{' '}
                    {binding.last_error}
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
        </div>
      </div>

      <EmbedBindingDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title={binding ? 'Edit application' : 'Embed in an application'}
        description="The authority publishes this sub-agent's definition under /.well-known/agent-skills/. Users whose access token carries one of the client ids below get this sub-agent."
        submitLabel={binding ? 'Save and sync' : 'Embed and sync'}
        initial={binding ? { baseUrl: binding.base_url, azps: binding.azps } : undefined}
        pending={setMutation.isPending}
        onSubmit={(body) => setMutation.mutate({ path: { sub_agent_id: subAgentId }, body })}
      />

      <Dialog open={confirmRemove} onOpenChange={setConfirmRemove}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Stop embedding?</DialogTitle>
            <DialogDescription>
              The sub-agent keeps its current versions and becomes editable here again. Syncing stops, and users of
              these client ids are no longer activated automatically.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmRemove(false)} disabled={removeMutation.isPending}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => removeMutation.mutate({ path: { sub_agent_id: subAgentId } })}
              disabled={removeMutation.isPending}
            >
              {removeMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Stop embedding
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  );
}
