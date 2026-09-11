import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { parseAzps } from '@/components/subagents/embedBinding';
import type { EmbedBindingUpsert } from '@/api/generated/types.gen';

interface EmbedBindingDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  submitLabel: string;
  /** Prefill when editing an existing binding. */
  initial?: { baseUrl: string; azps: string[] };
  pending?: boolean;
  onSubmit: (values: EmbedBindingUpsert) => void;
}

/**
 * The host binding form (ADR-0006): the origin that serves `/.well-known/agent-skills/`
 * and the OAuth client ids whose tokens belong to that host. Used both to bind an
 * existing sub-agent and to create one from the host's definition.
 */
export function EmbedBindingDialog({
  open,
  onOpenChange,
  title,
  description,
  submitLabel,
  initial,
  pending = false,
  onSubmit,
}: EmbedBindingDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {/* DialogContent unmounts when closed, so the form state resets on every open. */}
        <EmbedBindingForm
          initial={initial}
          pending={pending}
          submitLabel={submitLabel}
          onCancel={() => onOpenChange(false)}
          onSubmit={onSubmit}
        />
      </DialogContent>
    </Dialog>
  );
}

interface EmbedBindingFormProps {
  initial?: { baseUrl: string; azps: string[] };
  pending: boolean;
  submitLabel: string;
  onCancel: () => void;
  onSubmit: (values: EmbedBindingUpsert) => void;
}

function EmbedBindingForm({ initial, pending, submitLabel, onCancel, onSubmit }: EmbedBindingFormProps) {
  const [baseUrl, setBaseUrl] = useState(initial?.baseUrl ?? '');
  const [azpsText, setAzpsText] = useState(initial?.azps.join('\n') ?? '');

  const azps = parseAzps(azpsText);
  const canSubmit = baseUrl.trim().length > 0 && azps.length > 0 && !pending;

  const submit = () => {
    if (!canSubmit) return;
    onSubmit({ base_url: baseUrl.trim(), azps });
  };

  return (
    <>
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="embed-base-url">Authority origin</Label>
          <Input
            id="embed-base-url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://riad.alloy.ch"
            className="font-mono text-sm"
            autoComplete="off"
            autoFocus
          />
          <p className="text-xs text-muted-foreground">Scheme and host only. No path.</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="embed-azps">OAuth client ids (azp)</Label>
          <Textarea
            id="embed-azps"
            value={azpsText}
            onChange={(e) => setAzpsText(e.target.value)}
            placeholder="nannos-embedded"
            rows={3}
            className="font-mono text-sm resize-none"
          />
          <p className="text-xs text-muted-foreground">One per line. Each client id can belong to one embedded agent only.</p>
        </div>
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={!canSubmit}>
          {pending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
          {submitLabel}
        </Button>
      </DialogFooter>
    </>
  );
}
