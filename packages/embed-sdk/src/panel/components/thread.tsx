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
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import {
  BugIcon,
  CheckIcon,
  ChevronDownIcon,
  CopyIcon,
  DownloadIcon,
  FileIcon,
  FileTextIcon,
  ImageIcon,
  MessageCircleDashedIcon,
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
import { fetchWireHistory, textArrivalTs, textWire, textWireId } from '../../transport';
import type { NannosUIMessage, WireLogEntry } from '../../transport';
import { YamlView } from './yaml-view';
import { useChatEngineOptional } from '../engine';
import { useDevMode } from '../dev-mode';
import { toolPartTitle } from '../tool-title';
import { messagePlainText } from '../transcript';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';
import { AuthRequiredCard } from './auth-required-card';
import { ContextChip } from './context-chip';
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
          name: part.filename ?? part.url,
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
        'flex w-full min-w-0 flex-col rounded-md transition-colors',
        'has-[[data-slot=nannos-dev-wire-toggle]:hover]:bg-accent',
        open && 'bg-accent',
      )}
    >
      <div className="flex w-full min-w-0 items-start gap-1.5">
        <div className="min-w-0 flex-1">{children}</div>
        <button
          data-slot="nannos-dev-wire-toggle"
          type="button"
          aria-expanded={open}
          title="Wire detail — click for the raw source event"
          onClick={() => setOpen((v) => !v)}
          className="inline-flex shrink-0 items-center gap-0.5 rounded border border-amber-500/50 bg-amber-500/5 px-1 py-px text-left font-mono text-[10px] text-amber-700 hover:bg-amber-500/15 dark:text-amber-500"
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
  const rendered = renderAssistantPart(part, send, devMode);
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

function renderAssistantPart(
  part: MessagePart,
  send: UseNannosChatValue['send'],
  devMode: boolean,
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
        <ReasoningTrigger>
          <span aria-hidden="true">💭</span>
          <span className="truncate">{part.data.agent}</span>
          <ChevronDownIcon
            aria-hidden="true"
            className="size-3.5 transition-transform [[data-state=open]_&]:rotate-180"
          />
        </ReasoningTrigger>
        <ReasoningContent>{part.data.text}</ReasoningContent>
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
    // HITL parts are the thread's ONLY tool parts, and none of them belong in
    // the end-user view: a PENDING one surfaces through <ApprovalCard>, and an
    // ANSWERED one settles to a synthetic `{approved: true}` output — no result
    // anybody can read, and a reload drops the part entirely. So an approved
    // tool reads exactly like a tool that never needed approval: activity
    // lines, then the answer. Dev mode is the exception — it shows the raw part
    // (skipping the pending ones the card already renders), framed amber so it
    // clearly is not part of the end-user view.
    if (!devMode) return null;
    const isClientAction = (part.input as { _clientActionRequest?: boolean } | undefined)
      ?._clientActionRequest;
    if (part.state === 'approval-requested' && !isClientAction) return null;
    return (
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
    );
  }

  if (part.type === 'file') {
    return (
      <FileAttachment name={part.filename ?? part.url} mimeType={part.mediaType} url={part.url} />
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
  if (message.role === 'user') {
    const rendered = message.metadata?.display ? (
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
        {message.parts.map((part, index) => (
          <AssistantPart
            key={`${message.id}-${index}`}
            part={part}
            send={send}
            conversationId={conversationId}
          />
        ))}
        {showActions && <AssistantActions conversationId={conversationId} message={message} />}
      </div>
    );
  }
  return null;
}

export function Thread({ chat, className, showContinue = true }: ThreadProps) {
  const strings = useStrings();
  const lastMessage = chat.messages[chat.messages.length - 1];
  const lastHasStreamingText =
    lastMessage?.role === 'assistant' &&
    lastMessage.parts.some((part) => part.type === 'text' && part.text.trim().length > 0);
  const showBusy = chat.isBusy && !lastHasStreamingText;

  return (
    <Conversation data-slot="nannos-thread" className={cn('min-h-0', className)}>
      <ConversationContent className="gap-4">
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
            {chat.messages.map((message, index) => (
              <ThreadMessage
                key={message.id}
                message={message}
                conversationId={chat.conversationId}
                showActions={!(chat.isBusy && index === chat.messages.length - 1)}
                send={chat.send}
              />
            ))}
          </ConversationFeedbackProvider>
        )}

        {showBusy && (
          <div className="text-sm">
            <Shimmer>{strings['working.title']}</Shimmer>
          </div>
        )}
      </ConversationContent>
      <ConversationScrollButton data-slot="nannos-scroll-bottom" />
    </Conversation>
  );
}
