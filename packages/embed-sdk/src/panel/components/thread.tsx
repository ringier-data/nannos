/**
 * The message list: vendored ai-elements `conversation.tsx` (stick-to-bottom)
 * around a per-part renderer. User messages become bubbles (or a context chip
 * when host-injected), assistant messages render their parts in order —
 * markdown text, activity lines, agent thoughts, work plans, tool calls, file
 * attachments (download links), and the secondary-authorization prompt. Both
 * roles get a hover-revealed copy button for their readable text. An empty thread leads with
 * `<ContinueCard>` — the way back into the last conversation. Tool parts are
 * skipped here entirely: a pending approval renders as `<ApprovalCard>`, and an
 * answered one leaves nothing behind (dev mode aside) — the tool's own activity
 * lines already tell that story.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { useStickToBottomContext } from 'use-stick-to-bottom';
import {
  ArrowDownIcon,
  BugIcon,
  CheckIcon,
  ChevronDownIcon,
  CopyIcon,
  DownloadIcon,
  FileIcon,
  FileTextIcon,
  ImageIcon,
  MessageCircleDashedIcon,
  ShieldCheckIcon,
  XIcon,
} from 'lucide-react';
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from '../../components/ai-elements/conversation';
import { Message, MessageContent, MessageResponse } from '../../components/ai-elements/message';
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from '../../components/ai-elements/reasoning';
import { Shimmer } from '../../components/ai-elements/shimmer';
import { Tool, ToolContent, ToolHeader, ToolInput, ToolOutput } from '../../components/ai-elements/tool';
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { writeClipboard } from '../../lib/clipboard';
import { Tooltip, TooltipContent, TooltipTrigger } from '../../components/ui/tooltip';
import { format, useStrings } from '../../react';
import { fetchWireHistory, fileName, textArrivalTs, textWire, textWireId } from '../../transport';
import type { NannosUIMessage, WireLogEntry } from '../../transport';
import { YamlView } from './yaml-view';
import { useChatEngineOptional } from '../engine';
import { useDevMode } from '../dev-mode';
import { PAGE_COLUMN, usePanelLayout } from '../layout';
import { toolPartTitle } from '../tool-title';
import { messagePlainText } from '../transcript';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';
import { ApprovalCard } from './approval-card';
import { AuthRequiredCard } from './auth-required-card';
import { ContextChip } from './context-chip';
import { Receipt, ReceiptLine, type ReceiptOutcome } from './receipt';
import { ContinueCard } from './continue-card';
import { ConversationFeedbackProvider, MessageFeedback } from './message-feedback';
import { ReportIssueButton } from './report-issue-dialog';
import { WorkingBlock } from './working-block';

export interface ThreadProps {
  chat: UseNannosChatValue;
  className?: string;
  /**
   * Offer the way back into the last conversation over an empty thread.
   * Default: true. The panel turns it off in sidebar mode, where the whole
   * history is already on screen — two ways in would only fight each other.
   */
  showContinue?: boolean;
}

type MessagePart = NannosUIMessage['parts'][number];

interface AttachmentInfo {
  name: string;
  mimeType: string;
  /** Where the bytes are (a presigned URL the backend hydrates on load). A
   *  live send before its history reload may carry none. */
  url?: string;
}

/**
 * One attachment. With a URL it is a LINK — `download` for the save-as, a new
 * tab as the fallback for cross-origin presigned URLs the browser will not
 * save from directly — and an image also shows itself. Without one it is a
 * plain chip that names the file.
 */
function FileAttachment({ name, mimeType, url }: AttachmentInfo) {
  const strings = useStrings();
  const isImage = mimeType.startsWith('image/');
  const Icon = isImage ? ImageIcon : mimeType.startsWith('text/') ? FileTextIcon : FileIcon;
  const chip = (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-md border bg-secondary px-2 py-1 text-secondary-foreground text-xs',
        url && 'hover:bg-secondary/70 hover:underline',
      )}
    >
      <Icon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate">{name}</span>
      {url && <DownloadIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />}
    </span>
  );
  if (!url) return chip;
  return (
    <span data-slot="nannos-attachment" className="flex max-w-full flex-col items-start gap-1">
      {isImage && (
        <a href={url} target="_blank" rel="noopener noreferrer" className="max-w-full">
          <img src={url} alt={name} className="max-h-64 max-w-full rounded-md border object-contain" />
        </a>
      )}
      <a
        href={url}
        download={name}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={format(strings['message.download'], { name })}
        className="max-w-full"
      >
        {chip}
      </a>
    </span>
  );
}

/** Copy one message's readable text; the icon confirms for a moment. */
function CopyMessageButton({ text, className }: { text: string; className?: string }) {
  const strings = useStrings();
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const label =
    state === 'copied'
      ? strings['message.copied']
      : state === 'failed'
        ? strings['message.copyFailed']
        : strings['message.copy'];
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          data-slot="nannos-message-copy"
          data-state-copy={state}
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={label}
          className={cn('size-6 rounded-sm text-muted-foreground hover:text-foreground', className)}
          onClick={() => {
            void writeClipboard(text).then((ok) => {
              setState(ok ? 'copied' : 'failed');
              if (timer.current) clearTimeout(timer.current);
              timer.current = setTimeout(() => setState('idle'), 1500);
            });
          }}
        >
          {state === 'copied' ? (
            <CheckIcon className="size-3.5 text-green-600 dark:text-green-400" />
          ) : (
            <CopyIcon className="size-3.5" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

function UserMessage({ message }: { message: NannosUIMessage }) {
  const text = message.parts
    .filter((part): part is Extract<MessagePart, { type: 'text' }> => part.type === 'text')
    .map((part) => part.text)
    .join('\n');
  const fileParts = message.parts.filter(
    (part): part is Extract<MessagePart, { type: 'file' }> => part.type === 'file',
  );
  // Live sends carry files as wire metadata only; history carries `file` parts.
  const files: AttachmentInfo[] =
    fileParts.length > 0
      ? fileParts.map((part) => ({
          name: fileName(part),
          mimeType: part.mediaType,
          url: part.url,
        }))
      : (message.metadata?.attachments ?? []).map((att) => ({
          name: att.name,
          mimeType: att.mimeType,
          url: att.uri,
        }));

  // Nothing to say, nothing to draw: an empty user message (a HITL resume row
  // from history, say) would otherwise render as a blank bubble.
  if (!text && files.length === 0) return null;

  return (
    <div className="group/nannos-message flex w-full flex-col items-end gap-0.5">
      <Message from="user">
        <MessageContent>
          {text && <span className="whitespace-pre-wrap break-words">{text}</span>}
          {files.length > 0 && (
            <span className="flex flex-wrap gap-1.5">
              {files.map((file, index) => (
                <FileAttachment key={`${file.name}-${index}`} {...file} />
              ))}
            </span>
          )}
        </MessageContent>
      </Message>
      {/* Hover-revealed, like the assistant's row: the prompt is worth
          re-using as often as the answer is. */}
      {text && (
        <div
          data-slot="nannos-message-actions"
          className="flex items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover/nannos-message:opacity-100"
        >
          <CopyMessageButton text={text} />
        </div>
      )}
    </div>
  );
}

/**
 * Arrival time, dev mode only. Every event the agent sends carries one — the
 * activity lines in `data.ts`, the answer in its provider metadata — so the
 * turn reads as a timeline down the thread. Callers gate on dev mode; an
 * unstamped part (older history) renders nothing.
 */
function DevTimestamp({ ts }: { ts?: number }) {
  if (ts === undefined) return null;
  return (
    <span className="text-amber-600 text-xs dark:text-amber-500">
      {new Date(ts).toLocaleTimeString(undefined, { hour12: false })}
    </span>
  );
}

/**
 * Dev only: what a rendered part WAS on the wire. The demux stamps each part
 * with the wire log's label of the raw event that produced it (`data.wire`,
 * or `textArrival` metadata for text) — REAL values: kind, task state,
 * extension short names. A part restored from history carries no stamp and
 * falls back to the one label its shape guarantees.
 */
function wireLabel(part: MessagePart): string {
  switch (part.type) {
    case 'text':
      return textWire(part) ?? 'text';
    case 'data-activity':
      return part.data.wire ?? (part.data.kind === 'note' ? 'activity-log · note' : 'activity-log');
    case 'data-agent-thought':
      return part.data.wire ?? 'intermediate-output';
    case 'data-workplan':
      return part.data.wire ?? 'work-plan';
    case 'data-auth-required':
      return part.data.wire ?? 'auth-required';
    case 'dynamic-tool': {
      // Not stamped (tool chunks carry no metadata slot), but exact anyway:
      // these parts only ever come from an input-required status (demux.ts).
      const isClientAction = (part.input as { _clientActionRequest?: boolean } | undefined)
        ?._clientActionRequest;
      return isClientAction
        ? 'status-update · input-required · client-action'
        : `status-update · input-required · hitl · ${part.state}`;
    }
    case 'file':
      return 'file';
    default:
      return part.type;
  }
}

/** The wire-log entry id stamped onto a part — the key back to its raw event. */
function wireIdOf(part: MessagePart): string | undefined {
  switch (part.type) {
    case 'text':
      return textWireId(part);
    case 'data-activity':
    case 'data-agent-thought':
    case 'data-workplan':
    case 'data-auth-required':
      return part.data.wireId;
    default:
      return undefined;
  }
}

const devWirePre =
  'max-h-56 overflow-auto px-2 font-mono text-[10px] leading-snug text-foreground';
const devWireHint = 'pl-2 font-bold text-[10px] text-amber-700 dark:text-amber-500';

/**
 * Content + time to MATCH a history-restored part (no stamped id) to its wire
 * event once the log holds the record — this browser's own, or the backend's
 * after "load from server". Workplan and tool parts return nothing: snapshots
 * update in place, so no single event owns the final state.
 */
function sourceNeedle(part: MessagePart): { text: string; ts?: number } | undefined {
  switch (part.type) {
    case 'text':
      return { text: part.text, ts: textArrivalTs(part) };
    case 'data-activity':
      return { text: part.data.text, ts: part.data.ts };
    case 'data-agent-thought':
      return { text: part.data.text, ts: part.data.startedAt };
    case 'data-auth-required':
      return part.data.message ? { text: part.data.message } : undefined;
    default:
      return undefined;
  }
}

/** Pulls the backend's wire record right where the missing event is felt —
 *  the replay lands in the shared log, and the open expansion above rerenders
 *  with the match. Same fetch the inspector's wire tab uses. */
function DevWireLoadButton({ conversationId }: { conversationId: string }) {
  const engine = useChatEngineOptional();
  const [state, setState] = useState<'idle' | 'loading' | 'failed'>('idle');
  if (!engine) return null;
  return (
    <button
      type="button"
      disabled={state === 'loading'}
      className="self-start rounded border border-amber-500/50 bg-amber-500/5 px-1.5 py-0.5 font-mono text-[10px] text-amber-700 hover:bg-amber-500/15 disabled:opacity-50 dark:text-amber-500"
      onClick={() => {
        setState('loading');
        void fetchWireHistory(engine.adapter.api.fetch, conversationId).then((entries) => {
          if (!entries) {
            setState('failed');
            return;
          }
          engine.wireLog.replay(conversationId, entries);
          setState('idle');
        });
      }}
    >
      {state === 'loading'
        ? 'loading…'
        : state === 'failed'
          ? 'load failed — retry'
          : 'load the backend record'}
    </button>
  );
}

/**
 * The expanded badge body: the raw SOURCE EVENT and, under it, the UI part
 * the SDK distilled from it. The event is found by the id the demux stamped —
 * or, for a history-restored part with no stamp, MATCHED by content and time
 * against whatever the wire log holds. Subscribed to the log, so a record
 * loaded from the backend (here or in the inspector) fills the gap live.
 * Mounted only while open — closed badges cost no subscription.
 */
function DevWireExpansion({
  payload,
  wireId,
  needle,
  dir,
  conversationId,
}: {
  payload: unknown;
  wireId?: string;
  needle?: { text: string; ts?: number };
  dir: 'in' | 'out';
  conversationId: string;
}) {
  const engine = useChatEngineOptional();
  const noEntries = useMemo<WireLogEntry[]>(() => [], []);
  useSyncExternalStore(
    engine?.wireLog.subscribe ?? (() => () => {}),
    engine?.wireLog.getSnapshot ?? (() => noEntries),
  );
  // Exact first: the stamped id — a live client id, or the `srv:` row id the
  // history mapper shares with the server replay. Content matching remains
  // the fallback (a live id evicted from the ring buffer, older rows with no
  // id). Either can start unresolved and fill in when a record loads.
  const stamped = wireId ? engine?.wireLog.find(wireId) : undefined;
  const matched =
    !stamped && needle
      ? engine?.wireLog.findSource(conversationId, needle.text, needle.ts, dir)
      : undefined;
  const entry = stamped ?? matched;
  const canLoad = wireId?.startsWith('srv:') || needle !== undefined;
  return (
    <div className="m-2 mt-1 flex min-w-0 flex-col gap-1 rounded-md border bg-background p-1">
      <div className="border rounded-md">
      {entry ? (
        <>
          <span className={devWireHint}>
            source event · {entry.label}
            {entry.source && ` · ${entry.source}`}
            {matched && ' · matched by content'}
          </span>
          <YamlView value={entry.payload} className={devWirePre} />
        </>
      ) : canLoad ? (
        <>
          <span className={devWireHint}>source event not in the wire log yet</span>
          <DevWireLoadButton conversationId={conversationId} />
        </>
      ) : wireId ? (
        <span className={devWireHint}>
          source event no longer in the wire log (capacity) — see the dev inspector&apos;s wire tab
        </span>
      ) : null}
      </div>
      <div className="border rounded-md">
      <span className={devWireHint}>ui part</span>
      <YamlView value={payload} className={devWirePre} />
      </div>
    </div>
  );
}

/**
 * Dev only: rides on the SAME ROW as the part it describes — a small amber
 * badge naming the wire event behind the part. Hovering the badge (and the
 * whole time it is expanded) paints the part's row `bg-accent`, so the eye
 * knows exactly which rendered content the label talks about.
 */
function DevWirePart({
  label,
  payload,
  wireId,
  needle,
  dir = 'in',
  conversationId,
  children,
}: {
  label: string;
  payload: unknown;
  /** Wire-log entry of the source event; absent on history restores and sends. */
  wireId?: string;
  /** Content fallback for the source lookup when no id was stamped. */
  needle?: { text: string; ts?: number };
  /** 'in' for agent events; 'out' to match a user message to its send. */
  dir?: 'in' | 'out';
  conversationId: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  // Most specific first: the wire log says 'status-update · working ·
  // activity-log'; the badge reads better as 'activity-log · working ·
  // status-update' — and never truncates, the whole label IS the feature.
  const badgeLabel = label.split(' · ').reverse().join(' · ');
  return (
    <div
      data-slot="nannos-dev-wire"
      className={cn(
        '@container flex w-full min-w-0 flex-col rounded-md transition-colors',
        'has-[[data-slot=nannos-dev-wire-toggle]:hover]:bg-accent',
        open && 'bg-accent',
      )}
    >
      {/* The badge never truncates and must never take width from the content:
          in a docked panel it sits on its own line above, right-aligned; only
          when this wrapper is wide enough (a full-page chat) does it move
          beside the content. The wrapper is the container, so the threshold
          tracks the column the parts actually render in. */}
      <div className="flex w-full min-w-0 flex-col gap-0.5 @[40rem]:flex-row @[40rem]:items-start @[40rem]:gap-1.5">
        <div className="order-last min-w-0 @[40rem]:order-first @[40rem]:flex-1">{children}</div>
        <button
          data-slot="nannos-dev-wire-toggle"
          type="button"
          aria-expanded={open}
          title="Wire detail — click for the raw source event"
          onClick={() => setOpen((v) => !v)}
          className="inline-flex shrink-0 self-end items-center @[40rem]:ml-auto @[40rem]:self-start gap-0.5 rounded border border-amber-500/50 bg-amber-500/5 px-1 py-px text-left font-mono text-[10px] text-amber-700 hover:bg-amber-500/15 dark:text-amber-500"
        >
          <span>{badgeLabel}</span>
          <ChevronDownIcon
            aria-hidden="true"
            className={cn('size-2.5 shrink-0 transition-transform', open && 'rotate-180')}
          />
        </button>
      </div>
      {open && (
        <DevWireExpansion
          payload={payload}
          wireId={wireId}
          needle={needle}
          dir={dir}
          conversationId={conversationId}
        />
      )}
    </div>
  );
}

function AssistantPart({
  part,
  send,
  conversationId,
}: {
  part: MessagePart;
  send: UseNannosChatValue['send'];
  conversationId: string;
}) {
  const devMode = useDevMode();
  const strings = useStrings();
  const rendered = renderAssistantPart(part, send, devMode, strings);
  if (rendered === null || !devMode) return rendered;
  return (
    <DevWirePart
      label={wireLabel(part)}
      payload={part}
      wireId={wireIdOf(part)}
      needle={sourceNeedle(part)}
      conversationId={conversationId}
    >
      {rendered}
    </DevWirePart>
  );
}

/**
 * The turn's open interrupt, reachable from the part that raised it.
 * Null outside a thread (a part rendered in isolation, e.g. a test).
 */
const PendingInterruptContext = createContext<UseNannosChatValue['interrupt'] | null>(null);

/**
 * The approval card, at the position in the stream where the turn stopped.
 *
 * A batch arrives as several `approval-requested` parts but is ONE card — one
 * head, one set of batch actions — so it renders at the first of them and the
 * rest render nothing. Anchoring to the first (rather than the last) keeps the
 * card where the pause actually began.
 */
function PendingApprovalCard({ toolCallId }: { toolCallId: string }) {
  const interrupt = useContext(PendingInterruptContext);
  if (!interrupt || interrupt.pending.length === 0) return null;
  if (interrupt.pending[0].toolCallId !== toolCallId) return null;
  return <ApprovalCard interrupt={interrupt} />;
}

function renderAssistantPart(
  part: MessagePart,
  send: UseNannosChatValue['send'],
  devMode: boolean,
  strings: ReturnType<typeof useStrings>,
): ReactNode | null {
  if (part.type === 'text') {
    if (!part.text) return null;
    return (
      <Message from="assistant">
        <MessageContent className="gap-1">
          {devMode && <DevTimestamp ts={textArrivalTs(part)} />}
          <MessageResponse>{part.text}</MessageResponse>
        </MessageContent>
      </Message>
    );
  }

  if (part.type === 'data-activity') {
    // A mid-turn note is the agent SPEAKING while it works (`notify_user`), not a
    // machine label. It reads at answer size against an accent rule, so the eye
    // separates "understood, doing X" from the grey stream of tool lines around
    // it — and still stays in the timeline, where its order among those lines is
    // the whole point. Everything else keeps the muted micro-line.
    if (part.data.kind === 'note') {
      return (
        <div
          data-slot="nannos-agent-note"
          className="border-primary/40 text-foreground border-l-2 py-0.5 pl-2.5 text-sm"
        >
          {devMode && (
            <>
              <DevTimestamp ts={part.data.ts} />{' '}
            </>
          )}
          {part.data.source && <span className="font-medium">{part.data.source} › </span>}
          {part.data.text}
        </div>
      );
    }
    return (
      <div data-slot="nannos-activity" className="text-muted-foreground text-xs">
        {devMode && (
          <>
            <DevTimestamp ts={part.data.ts} />{' '}
          </>
        )}
        {part.data.source && <span className="font-medium">{part.data.source} › </span>}
        {part.data.text}
      </div>
    );
  }

  if (part.type === 'data-agent-thought') {
    return (
      <Reasoning data-slot="nannos-agent-thought" isStreaming={!part.data.complete} className="mb-0">
        {/* Same voice as every other line in the stream — "source › what": no
            icon, muted micro-type, the chevron the step group also uses. */}
        <ReasoningTrigger className="w-fit gap-1 text-xs">
          <span className="truncate">
            <span className="font-medium">{part.data.agent}</span> › {strings['thread.thinking']}
          </span>
          <ChevronDownIcon
            aria-hidden="true"
            className="size-3.5 transition-transform [[data-state=open]_&]:rotate-180"
          />
        </ReasoningTrigger>
        {/* Reasoning is context, not the answer: a step down in size and
            already muted, so the eye lands on the reply below it. */}
        <ReasoningContent
          className={cn(
            'mt-1 text-xs [&_p]:my-1 [&_h1]:text-xs [&_h2]:text-xs [&_h3]:text-xs',
            // Streamdown pins its own `text-sm` on table cells and inline code;
            // pull those down too, or a table in a thought lands at answer size.
            '[&_th]:px-3 [&_th]:py-1 [&_th]:text-xs [&_td]:px-3 [&_td]:py-1 [&_td]:text-xs [&_code]:text-xs',
          )}
        >
          {part.data.text}
        </ReasoningContent>
      </Reasoning>
    );
  }

  if (part.type === 'data-auth-required') {
    return <AuthRequiredCard data={part.data} send={send} />;
  }

  if (part.type === 'data-workplan') {
    return <WorkingBlock todos={part.data.todos} />;
  }

  if (part.type === 'dynamic-tool') {
    // HITL parts are the thread's ONLY tool parts. A PENDING one renders the
    // approval card RIGHT HERE — the turn stopped at this point in the stream,
    // so this is where the question belongs and where the answer will settle.
    // An ANSWERED one settles to a synthetic `{approved: true}` output — no
    // result anybody can read — so it leaves a receipt instead: the decision,
    // and nothing about the tool that a reader could mistake for its outcome.
    // Dev mode ADDS the raw part beneath, framed amber so it clearly is not
    // part of the end-user view — it never replaces the card. It used to, back
    // when the card was docked in the panel and survived independently; now the
    // card is the thread's job and the only way to answer, so hiding it behind
    // the raw part left a dev-mode session unable to decide anything.
    const isRoundTrip = (part.input as { _clientActionRequest?: boolean } | undefined)
      ?._clientActionRequest;
    let endUser: ReactNode = null;
    if (part.state === 'approval-requested') {
      // Client-action round trips pause here too, but the SDK answers those
      // itself — a card would ask the user about work already under way.
      endUser = isRoundTrip ? null : <PendingApprovalCard toolCallId={part.toolCallId} />;
    } else {
      // The turn pauses at the card and resumes with more steps: without a line
      // in between, a reader cannot tell why the work broke off or that they
      // decided anything.
      const outcome: ReceiptOutcome | null =
        part.state === 'output-available'
          ? 'approved'
          : part.state === 'output-denied'
            ? 'rejected'
            : null;
      endUser = outcome ? (
        <Receipt outcome={outcome} subject={toolPartTitle(part.toolName, part.input)} />
      ) : null;
    }
    if (!devMode) return endUser;
    return (
      <div className="flex flex-col gap-1">
        {endUser}
        <div className="rounded-lg border border-amber-500/50 border-dashed bg-amber-500/5 p-1">
          <span className="flex items-center gap-1 px-1 pb-1 font-medium text-amber-700 text-xs dark:text-amber-500">
            <BugIcon aria-hidden="true" className="size-3 shrink-0" /> dev only
          </span>
          <Tool data-slot="nannos-tool">
            <ToolHeader
              type="dynamic-tool"
              state={part.state}
              toolName={part.toolName}
              title={toolPartTitle(part.toolName, part.input)}
            />
            <ToolContent>
              {part.input !== undefined && <ToolInput input={part.input} />}
              <ToolOutput output={part.output} errorText={part.errorText} />
            </ToolContent>
          </Tool>
        </div>
      </div>
    );
  }

  if (part.type === 'file') {
    return (
      <FileAttachment name={fileName(part)} mimeType={part.mediaType} url={part.url} />
    );
  }

  // step-start and any unknown part types render nothing.
  return null;
}

/** Hover-revealed feedback + report row under a COMPLETED assistant message. */
function AssistantActions({
  conversationId,
  message,
}: {
  conversationId: string;
  message: NannosUIMessage;
}) {
  // Feedback endpoints expect the DB id; live messages carry it as metadata.
  const messageId = message.metadata?.persistedMessageId ?? message.id;
  const text = messagePlainText(message);
  return (
    <div
      data-slot="nannos-message-actions"
      className="-mt-1 flex items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover/nannos-message:opacity-100 has-[[data-rated]]:opacity-100"
    >
      {text && <CopyMessageButton text={text} />}
      <MessageFeedback conversationId={conversationId} messageId={messageId} />
      <ReportIssueButton conversationId={conversationId} messageId={messageId} />
    </div>
  );
}

type ActivityPart = Extract<MessagePart, { type: 'data-activity' | 'dynamic-tool' }>;

/**
 * A machine line in the activity stream: a tool label, or the approve/reject
 * acknowledgement an answered HITL tool renders as — a note is the agent
 * speaking, not one of these.
 */
function isMachineActivity(part: MessagePart): part is ActivityPart {
  if (part.type === 'dynamic-tool') return part.state !== 'approval-requested';
  return part.type === 'data-activity' && part.data.kind !== 'note';
}

/** The text a folded group shows while its turn is still running. */
function latestActivityText(parts: ActivityPart[]): string | undefined {
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const part = parts[i];
    if (part.type === 'data-activity') return part.data.text;
  }
  return undefined;
}

/**
 * A "thought" that is the answer, word for word. When the model replies in
 * plain text instead of calling the response tool, the orchestrator routes
 * that text to its thinking channel and the fallback then surfaces the same
 * text as the final message — the thought block becomes a verbatim copy of the
 * reply above the reply. Nothing to think about in there: drop it. Real
 * reasoning never equals the answer, so a strict trimmed comparison is enough.
 */
function withoutEchoedThoughts(parts: MessagePart[]): MessagePart[] {
  const answers = parts
    .filter((part): part is Extract<MessagePart, { type: 'text' }> => part.type === 'text')
    .map((part) => part.text.trim())
    .filter(Boolean);
  if (answers.length === 0) return parts;
  return parts.filter(
    (part) =>
      !(part.type === 'data-agent-thought' && part.data.complete && answers.includes(part.data.text.trim())),
  );
}

/**
 * Page layout folds each run of consecutive machine lines into one group;
 * everything else (and every part in panel/dev mode) passes through as-is, so
 * a note or an answer between two runs keeps its place in the timeline.
 */
function groupActivity(parts: MessagePart[], fold: boolean): Array<MessagePart | ActivityPart[]> {
  if (!fold) return parts;
  const out: Array<MessagePart | ActivityPart[]> = [];
  for (const part of parts) {
    const last = out[out.length - 1];
    if (isMachineActivity(part)) {
      if (Array.isArray(last)) last.push(part);
      else out.push([part]);
    } else {
      out.push(part);
    }
  }
  return out;
}

/**
 * The folded activity stream: one summary line the reader can open. Stays
 * open while the turn is in progress — that is when "what is it doing" matters
 * — and collapses to the summary once the answer has landed.
 */
/**
 * "1 approved, 1 rejected" for the collapsed label, or '' when the run holds no
 * decisions. Counts settled HITL parts only: a pending one renders its card
 * outside the group, and an activity line is not a decision.
 */
function countDecisions(parts: ActivityPart[], strings: ReturnType<typeof useStrings>): string {
  let approved = 0;
  let rejected = 0;
  for (const part of parts) {
    if (part.type !== 'dynamic-tool') continue;
    if (part.state === 'output-available') approved += 1;
    else if (part.state === 'output-denied') rejected += 1;
  }
  return [
    approved > 0 ? format(strings['thread.activityApproved'], { count: approved }) : null,
    rejected > 0 ? format(strings['thread.activityRejected'], { count: rejected }) : null,
  ]
    .filter(Boolean)
    .join(', ');
}

function ActivityGroup({
  parts,
  send,
  conversationId,
  inProgress,
}: {
  parts: ActivityPart[];
  send: UseNannosChatValue['send'];
  conversationId: string;
  inProgress: boolean;
}) {
  const strings = useStrings();
  const [open, setOpen] = useState(false);
  const expanded = inProgress || open;
  const steps =
    parts.length === 1
      ? strings['thread.activityStep']
      : format(strings['thread.activitySteps'], { count: parts.length });
  // Receipts fold with the rest of the steps — but a decision the user made must
  // never disappear behind a chevron unannounced, so the collapsed label counts
  // them. Nothing is appended when the group holds no decisions.
  const decisions = countDecisions(parts, strings);
  const label = decisions ? `${steps} · ${decisions}` : steps;
  return (
    <div data-slot="nannos-activity-group" className="flex flex-col gap-1">
      <button
        type="button"
        className="inline-flex w-fit items-center gap-1 text-muted-foreground text-xs hover:text-foreground"
        aria-expanded={expanded}
        onClick={() => setOpen((value) => !value)}
      >
        {inProgress ? <Shimmer>{latestActivityText(parts) ?? label}</Shimmer> : label}
        <ChevronDownIcon
          aria-hidden="true"
          className={cn('size-3.5 shrink-0 transition-transform', expanded && 'rotate-180')}
        />
      </button>
      {expanded && (
        <div className="flex flex-col gap-1 border-l-2 border-border pl-2.5">
          {parts.map((part, index) => (
            <AssistantPart key={index} part={part} send={send} conversationId={conversationId} />
          ))}
        </div>
      )}
    </div>
  );
}

function ThreadMessage({
  message,
  conversationId,
  showActions,
  send,
}: {
  message: NannosUIMessage;
  conversationId: string;
  /** False while this is the streaming last message of a busy turn. */
  showActions: boolean;
  /** Reaches the authorization card, whose confirm button sends a turn. */
  send: UseNannosChatValue['send'];
}) {
  const devMode = useDevMode();
  const layout = usePanelLayout();
  const strings = useStrings();
  if (message.role === 'user') {
    // A `receipt` display means the PANEL composed this turn to resume the
    // agent after the user decided something in a card. It is not a sentence
    // the user typed, so it reads as an activity receipt rather than a bubble
    // or a context chip — the prompt itself stays visible in dev mode.
    const receiptOutcome = message.metadata?.display?.outcome ?? 'authorized';
    const rendered =
      message.metadata?.display?.kind === 'receipt' ? (
        <ReceiptLine
          icon={receiptOutcome === 'skipped' ? XIcon : ShieldCheckIcon}
          outcome={receiptOutcome}
        >
          <span className="font-medium">{message.metadata.display.label}</span>
          {/* Authorizing is the middle of the story — the agent was asked to try
              again. A refusal is the end of it, and says nothing more. */}
          {receiptOutcome === 'authorized' && (
            <>
              {' · '}
              {strings['receipt.retried']}
            </>
          )}
        </ReceiptLine>
      ) : message.metadata?.display ? (
        <ContextChip message={message} />
      ) : (
        <UserMessage message={message} />
      );
    if (!devMode) return rendered;
    // The badge wraps even a message whose bubble renders nothing (a HITL
    // resume row, say) — dev mode is exactly where the invisible send should
    // still leave a trace.
    const userText = message.parts.find(
      (part): part is Extract<MessagePart, { type: 'text' }> => part.type === 'text',
    )?.text;
    return (
      <DevWirePart
        label={message.metadata?.display ? 'message · user · context' : 'message · user'}
        payload={{ id: message.id, parts: message.parts, metadata: message.metadata }}
        needle={userText ? { text: userText } : undefined}
        dir="out"
        conversationId={conversationId}
      >
        {rendered}
      </DevWirePart>
    );
  }
  if (message.role === 'assistant') {
    return (
      <div className="group/nannos-message flex w-full flex-col gap-2">
        {groupActivity(devMode ? message.parts : withoutEchoedThoughts(message.parts), layout === 'page' && !devMode).map((item, index) =>
          Array.isArray(item) ? (
            <ActivityGroup
              key={`${message.id}-${index}`}
              parts={item}
              send={send}
              conversationId={conversationId}
              inProgress={!showActions}
            />
          ) : (
            <AssistantPart
              key={`${message.id}-${index}`}
              part={item}
              send={send}
              conversationId={conversationId}
            />
          ),
        )}
        {showActions && <AssistantActions conversationId={conversationId} message={message} />}
      </div>
    );
  }
  return null;
}

/**
 * One turn, one block. A HITL pause ends the assistant message the stream was
 * building and the resume opens a new one, so a turn that asked for approval
 * arrives as two consecutive assistant messages — and reads as two answers,
 * with a feedback row in the middle of the work. History already stitches
 * these back together on reload (`rowsToUIMessages`); do the same live, at
 * render time, keeping the LAST message's identity so feedback lands on the
 * row the backend finalised.
 */
export function mergeAssistantRuns(messages: NannosUIMessage[]): NannosUIMessage[] {
  const out: NannosUIMessage[] = [];
  for (const message of messages) {
    const last = out[out.length - 1];
    if (message.role === 'assistant' && last?.role === 'assistant') {
      out[out.length - 1] = {
        ...message,
        id: message.id,
        parts: [...last.parts, ...message.parts],
        metadata: { ...last.metadata, ...message.metadata },
      };
    } else {
      out.push(message);
    }
  }
  return out;
}

export function Thread({ chat, className, showContinue = true }: ThreadProps) {
  const strings = useStrings();
  const layout = usePanelLayout();
  const lastMessage = chat.messages[chat.messages.length - 1];
  const lastHasStreamingText =
    lastMessage?.role === 'assistant' &&
    lastMessage.parts.some((part) => part.type === 'text' && part.text.trim().length > 0);
  const showBusy = chat.isBusy && !lastHasStreamingText;

  return (
    <Conversation data-slot="nannos-thread" className={cn('min-h-0', className)}>
      <ConversationContent className={cn('gap-4', layout === 'page' && `${PAGE_COLUMN} gap-6 py-6`)}>
        {chat.hasOlderMessages && (
          <div className="flex justify-center">
            <Button
              data-slot="nannos-load-older"
              type="button"
              variant="ghost"
              size="sm"
              className="text-muted-foreground text-xs"
              onClick={() => void chat.loadOlderMessages()}
            >
              {strings['thread.loadOlder']}
            </Button>
          </div>
        )}

        {chat.error && (
          <Alert data-slot="nannos-thread-error" variant="destructive">
            <AlertTitle>{strings['thread.error']}</AlertTitle>
            <AlertDescription className="break-words">{chat.error.message}</AlertDescription>
          </Alert>
        )}

        {chat.messages.length === 0 && !showBusy ? (
          <>
            {/* Sits ABOVE the empty state, not inside it: the way back into the
                last conversation is the first thing on an empty thread, while
                the invitation to type stays centred under it. */}
            {showContinue && <ContinueCard className="shrink-0" />}
            <ConversationEmptyState
              title={strings['thread.emptyTitle']}
              description={strings['thread.emptyHint']}
            />
          </>
        ) : (
          <ConversationFeedbackProvider conversationId={chat.conversationId}>
            {/* The approval card renders deep inside a message's parts, at the
                point the turn stopped. Threading the interrupt down through
                every part renderer to reach it would touch code that has no
                interest in approvals; a context puts it exactly where it is
                consumed. */}
            <PendingInterruptContext.Provider value={chat.interrupt}>
              {mergeAssistantRuns(chat.messages).map((message, index, merged) => (
                <ThreadMessage
                  key={message.id}
                  message={message}
                  conversationId={chat.conversationId}
                  showActions={!(chat.isBusy && index === merged.length - 1)}
                  send={chat.send}
                />
              ))}
            </PendingInterruptContext.Provider>
          </ConversationFeedbackProvider>
        )}

        {showBusy && (
          <div className="text-sm">
            <Shimmer>{strings['working.title']}</Shimmer>
          </div>
        )}
      </ConversationContent>
      {/* An inline card can be scrolled past — the docked one could not. The
          pill is the way BACK to it, not a second copy of it, and it takes the
          scroll button's slot rather than stacking on top of it. */}
      {chat.interrupt.pending.length > 0 ? (
        <PendingApprovalPill count={chat.interrupt.pending.length} />
      ) : (
        <ConversationScrollButton data-slot="nannos-scroll-bottom" />
      )}
    </Conversation>
  );
}

/**
 * The jump back to a pending interrupt. Only while one is open and the user has
 * scrolled away from it: the composer stays live throughout, so scrolling down
 * to type is a normal thing to do and must not strand the request.
 */
function PendingApprovalPill({ count }: { count: number }) {
  const strings = useStrings();
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  if (isAtBottom) return null;
  return (
    <Button
      data-slot="nannos-pending-approvals"
      type="button"
      size="sm"
      className="-translate-x-1/2 absolute bottom-4 left-[50%] h-7 gap-1.5 rounded-full px-3 text-xs shadow-md"
      onClick={() => scrollToBottom()}
    >
      <ArrowDownIcon aria-hidden="true" className="size-3" />
      {count === 1
        ? strings['thread.pendingOne']
        : format(strings['thread.pendingMany'], { count: String(count) })}
    </Button>
  );
}
