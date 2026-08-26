/**
 * The message list: vendored ai-elements `conversation.tsx` (stick-to-bottom)
 * around a per-part renderer. User messages become bubbles (or a context chip
 * when host-injected), assistant messages render their parts in order —
 * markdown text, activity lines, agent thoughts, work plans, tool calls, file
 * chips, and the secondary-authorization prompt. An empty thread leads with
 * `<ContinueCard>` — the way back into the last conversation. Tool parts are
 * skipped here entirely: a pending approval renders as `<ApprovalCard>`, and an
 * answered one leaves nothing behind (dev mode aside) — the tool's own activity
 * lines already tell that story.
 */
import { BugIcon, ChevronDownIcon, FileIcon, FileTextIcon, ImageIcon, MessageCircleDashedIcon } from 'lucide-react';
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
import { useStrings } from '../../react';
import { textArrivalTs } from '../../transport';
import type { NannosUIMessage } from '../../transport';
import { useDevMode } from '../dev-mode';
import { toolPartTitle } from '../tool-title';
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

function FileChip({ name, mimeType }: { name: string; mimeType: string }) {
  const Icon = mimeType.startsWith('image/')
    ? ImageIcon
    : mimeType.startsWith('text/')
      ? FileTextIcon
      : FileIcon;
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border bg-secondary px-2 py-1 text-secondary-foreground text-xs">
      <Icon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate">{name}</span>
    </span>
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
  const files =
    fileParts.length > 0
      ? fileParts.map((part) => ({
          name: part.filename ?? part.url,
          mimeType: part.mediaType,
        }))
      : (message.metadata?.attachments ?? []).map((att) => ({
          name: att.name,
          mimeType: att.mimeType,
        }));

  // Nothing to say, nothing to draw: an empty user message (a HITL resume row
  // from history, say) would otherwise render as a blank bubble.
  if (!text && files.length === 0) return null;

  return (
    <Message from="user">
      <MessageContent>
        {text && <span className="whitespace-pre-wrap break-words">{text}</span>}
        {files.length > 0 && (
          <span className="flex flex-wrap gap-1.5">
            {files.map((file, index) => (
              <FileChip key={`${file.name}-${index}`} name={file.name} mimeType={file.mimeType} />
            ))}
          </span>
        )}
      </MessageContent>
    </Message>
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

function AssistantPart({ part, send }: { part: MessagePart; send: UseNannosChatValue['send'] }) {
  const devMode = useDevMode();

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
    return <FileChip name={part.filename ?? part.url} mimeType={part.mediaType} />;
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
  return (
    <div
      data-slot="nannos-message-actions"
      className="-mt-1 flex items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover/nannos-message:opacity-100 has-[[data-rated]]:opacity-100"
    >
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
  if (message.role === 'user') {
    if (message.metadata?.display) return <ContextChip message={message} />;
    return <UserMessage message={message} />;
  }
  if (message.role === 'assistant') {
    return (
      <div className="group/nannos-message flex w-full flex-col gap-2">
        {message.parts.map((part, index) => (
          <AssistantPart key={`${message.id}-${index}`} part={part} send={send} />
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
