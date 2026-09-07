/**
 * Bug reporting (ported from the v1 ReportIssueDialog): a small flag button
 * that opens a dialog with a free-text description, submitted through
 * `adapter.api.reportIssue`. That endpoint is OPTIONAL on the adapter — hosts
 * without a bug-report backend omit it and the button renders nothing.
 * Success/failure is routed through `adapter.notify` when the host provides
 * one; failure additionally shows inline so it is never silent.
 */
import { useState } from 'react';
import { FlagIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog';
import { Spinner } from '../../components/ui/spinner';
import { Textarea } from '../../components/ui/textarea';
import { Tooltip, TooltipContent, TooltipTrigger } from '../../components/ui/tooltip';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useChatEngine } from '../engine';

export interface ReportIssueButtonProps {
  conversationId: string;
  /** The PERSISTED message id the report is about, when message-scoped. */
  messageId?: string;
  className?: string;
}

export function ReportIssueButton({ conversationId, messageId, className }: ReportIssueButtonProps) {
  const strings = useStrings();
  const { adapter } = useChatEngine();
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState('');
  const [isPending, setIsPending] = useState(false);
  const [failed, setFailed] = useState(false);

  const reportIssue = adapter.api.reportIssue;
  // No backend for reports → no affordance at all.
  if (!reportIssue) return null;

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) {
      setDescription('');
      setFailed(false);
    }
  };

  const handleSubmit = async () => {
    setIsPending(true);
    setFailed(false);
    try {
      const ok = await reportIssue({
        conversationId,
        ...(messageId && { messageId }),
        description: description || undefined,
      });
      if (ok) {
        adapter.notify?.('success', strings['report.success']);
        handleOpenChange(false);
      } else {
        setFailed(true);
        adapter.notify?.('error', strings['report.error']);
      }
    } catch {
      setFailed(true);
      adapter.notify?.('error', strings['report.error']);
    } finally {
      setIsPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            data-slot="nannos-report-issue"
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={strings['report.title']}
            className={cn('size-6 rounded-sm text-muted-foreground hover:text-foreground', className)}
            onClick={() => setOpen(true)}
          >
            <FlagIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{strings['report.title']}</TooltipContent>
      </Tooltip>

      <DialogContent data-slot="nannos-report-issue-dialog">
        <DialogHeader>
          <DialogTitle>{strings['report.title']}</DialogTitle>
          <DialogDescription>{strings['report.description']}</DialogDescription>
        </DialogHeader>
        <Textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={strings['report.placeholder']}
          rows={4}
          disabled={isPending}
          className="resize-none"
        />
        {failed && <p className="text-destructive text-sm">{strings['report.error']}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={isPending} onClick={() => handleOpenChange(false)}>
            {strings['report.cancel']}
          </Button>
          <Button type="button" disabled={isPending} onClick={() => void handleSubmit()}>
            {isPending && <Spinner className="size-4" />}
            {strings['report.submit']}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
