/**
 * The per-conversation chat facade: `useChat` over the scope's held `Chat`
 * instance, plus everything the old ChatContext did around it — history
 * seeding, keyset pagination, steering (send-while-streaming), the HITL
 * interrupt surface, and the seeded-prompt drain.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { directiveFromToolArgs, generateUUID, settleRegistrations } from '../../core';
import { useChat } from '@ai-sdk/react';
import {
  answeredInChatPart,
  encodeApproval,
  wireCallId,
  endsWithOpenPrompt,
  rowsToUIMessages,
  type NannosUIMessage,
  type ReviewConfig,
  type RestMessageRow,
  type TodoItem,
} from '../../transport';
import { useAssistant } from '../../react';
import { useChatEngine } from '../engine';
import { CLIENT_ACTION_TOOL } from '../tool-title';
import { useConversations } from './use-conversations';

const MESSAGE_PAGE_SIZE = 100;

const NAVIGATED_KEY_PREFIX = 'nannos:navigated:';

/**
 * A navigate runs at most once per request, across remounts. Navigating can
 * unmount the very panel that ran it (a host route that swaps out the layout)
 * before the result is sent; the remount restores the unanswered request and
 * would navigate again — forever. So the request is recorded before it runs,
 * and a replay only reports the page the first run landed on. Other kinds
 * replay as before: re-running them after a reload is the recovery.
 */
export function replayableDirective(approvalId: string, directive: unknown): unknown {
  if ((directive as { kind?: unknown } | null)?.kind !== 'navigate') return directive;
  const key = `${NAVIGATED_KEY_PREFIX}${approvalId}`;
  try {
    if (sessionStorage.getItem(key)) return { kind: 'read_current_page' };
    sessionStorage.setItem(key, '1');
  } catch {
    /* no storage: the in-memory guard still covers re-renders */
  }
  return directive;
}

export interface PendingApproval {
  toolCallId: string;
  toolName: string;
  input: Record<string, unknown>;
  approvalId: string;
}

export interface UseNannosChatValue {
  conversationId: string;
  messages: NannosUIMessage[];
  status: 'submitted' | 'streaming' | 'ready' | 'error';
  error: Error | undefined;
  /**
   * Send, or STEER when a turn is already running. `interrupt: true` cancels
   * the running turn first and starts a fresh one with this message instead
   * (the composer's "stop and send" mode).
   */
  send: (
    text: string,
    opts?: {
      displayText?: string;
      /**
       * How `displayText` reads in the thread. `context` (the default) is a
       * host-injected chip — something the PAGE contributed on the user's
       * behalf. `receipt` is a decision the user made in a card: it renders as
       * an activity receipt, because a turn the panel sent to resume the agent
       * is not a sentence the user typed and must not pose as one.
       */
      displayKind?: 'context' | 'receipt';
      /** Which receipt this turn reads as, when `displayKind` is `receipt`. */
      displayOutcome?: 'authorized' | 'skipped';
      /** The user's answer to an `auth-required` prompt, sent as a DataPart. */
      authorization?: { decision: 'approved' | 'declined'; message?: string };
      files?: Array<{ uri: string; mimeType: string; name: string; s3Url?: string }>;
      interrupt?: boolean;
    },
  ) => void;
  stop: () => Promise<void>;
  /** The current interrupt: approval-requested dynamic-tool parts + gating. */
  interrupt: {
    pending: PendingApproval[];
    reason: string | undefined;
    reviewConfigs: ReviewConfig[];
    respond: (approvalId: string, approved: boolean, reason?: string) => Promise<void>;
  };
  /** Live work plan of the (streaming) turn — last workplan part wins. */
  workingSteps: TodoItem[];
  isBusy: boolean;
  isReadOnly: boolean;
  loadOlderMessages: () => Promise<void>;
  hasOlderMessages: boolean;
}

export function useNannosChat(conversationIdOverride?: string): UseNannosChatValue {
  const engine = useChatEngine();
  const assistant = useAssistant();
  const { activeConversationId } = useConversations();

  // A surface with no active conversation starts one: the id is minted during
  // render (stable ref — ids are cheap and never change), the store learns
  // about it in an effect (mutating a subscribable store mid-render would
  // setState other components).
  const mintedIdRef = useRef<string | null>(null);
  if (!conversationIdOverride && !activeConversationId && !mintedIdRef.current) {
    mintedIdRef.current = generateUUID();
  }
  const conversationId = conversationIdOverride ?? activeConversationId ?? mintedIdRef.current!;
  useEffect(() => {
    if (mintedIdRef.current && !activeConversationId && !conversationIdOverride) {
      engine.conversations.adopt(mintedIdRef.current);
    }
  }, [activeConversationId, conversationIdOverride, engine]);

  const chat = engine.getOrCreateChat(conversationId);
  const { messages, status, error, sendMessage, stop, addToolApprovalResponse, setMessages } =
    useChat<NannosUIMessage>({ chat });

  const isReadOnly = engine.conversations.isReadOnly(conversationId);

  // --- history seeding + reconnect --------------------------------------------
  const seededRef = useRef(new Set<string>());
  useEffect(() => {
    if (seededRef.current.has(conversationId)) return;
    seededRef.current.add(conversationId);
    if (chat.messages.length > 0 || engine.transport.hasActiveTurn(conversationId)) return;
    // Cancelled before the page landed (React runs mount → cleanup → mount over
    // the same hook in StrictMode; a fast switch away does the same): forget the
    // mark, or the re-run bails out above and the conversation the panel opened
    // on stays blank for good — its row in the list is already selected, so
    // picking it again changes nothing.
    let seeded = false;
    // A conversation created in this browser has no server side yet: no history
    // to fetch, and `subscribe_conversation` would be rejected — the resume
    // probe would then sit open for its whole timeout, and a send inside that
    // window steers into a turn that does not exist.
    if (engine.conversations.isLocalOnly(conversationId)) return;
    let cancelled = false;
    void (async () => {
      const rows = await fetchMessagePage(engine.adapter.api.fetch, conversationId, null);
      if (cancelled || !rows) return;
      engine.conversations.setPageState(conversationId, {
        cursor: rows.nextCursor,
        hasMore: !!rows.nextCursor,
      });
      seeded = true;
      if (rows.items.length > 0 && chat.messages.length === 0) {
        // A prompt still open at the end comes back `approval-requested` — the
        // pending interrupt, card and client-action auto-settle included.
        chat.messages = rowsToUIMessages(rows.items);
      }
      // Paused at a card that is already on screen: nothing to resume, and the
      // snapshot's replay of that prompt would render it a second time.
      if (endsWithOpenPrompt(chat.messages)) return;
      // Rejoin the stream room; an in-flight turn resumes via the snapshot.
      void chat.resumeStream();
    })();
    return () => {
      cancelled = true;
      if (!seeded) seededRef.current.delete(conversationId);
    };
  }, [chat, conversationId, engine]);

  // --- seeded prompt drain (sendOnOpen only; drafts are the composer's) --------
  const seededPrompt = assistant.seededPrompt;
  useEffect(() => {
    if (!seededPrompt?.sendOnOpen || isReadOnly) return;
    // A keyed prompt about a DIFFERENT page context starts a fresh conversation;
    // `newConversation` starts one regardless (idempotent: a fresh-but-blank
    // active target is reused, so the re-render after retargeting settles here).
    const target = engine.conversations.resolveTarget(seededPrompt.contextKey, {
      fresh: seededPrompt.newConversation,
    });
    if (target !== conversationId) return; // re-render picks up the new target
    assistant.clearSeededPrompt();
    void sendMessage({
      text: seededPrompt.text,
      metadata: {
        sentAt: Date.now(),
        ...(seededPrompt.displayText && {
          display: { kind: 'context' as const, label: seededPrompt.displayText },
        }),
      },
    });
    engine.conversations.noteTitle(conversationId, seededPrompt.displayText ?? seededPrompt.text);
  }, [seededPrompt, conversationId, isReadOnly, engine, assistant, sendMessage]);

  // --- actions ------------------------------------------------------------------
  const send = useCallback<UseNannosChatValue['send']>(
    (text, opts) => {
      if (!text.trim() || isReadOnly) return;
      const active =
        engine.transport.hasActiveTurn(conversationId) ||
        status === 'streaming' ||
        status === 'submitted';
      const startTurn = () => {
        // A reply TYPED while an approval card waits IS its answer — the server
        // reads the words, and reports the reading (`data-hitl-decision`). Settle
        // the card now so its buttons go inactive at once; it reads "answered in
        // chat" until that reading lands. A panel-composed receipt is not one.
        if (opts?.displayKind !== 'receipt') {
          setMessages((prev) => {
            const last = [...prev].reverse().find((m) => m.role === 'assistant');
            const awaiting = (p: (typeof prev)[number]['parts'][number]) =>
              p.type === 'dynamic-tool' &&
              p.state === 'approval-requested' &&
              !(p.input as { _clientActionRequest?: boolean } | undefined)?._clientActionRequest;
            if (!last?.parts.some(awaiting)) return prev;
            return prev.map((m) =>
              m !== last
                ? m
                : {
                    ...m,
                    parts: m.parts.map((p) => (p.type === 'dynamic-tool' && awaiting(p) ? answeredInChatPart(p) : p)),
                  },
            );
          });
        }
        engine.conversations.noteTitle(conversationId, opts?.displayText ?? text);
        void sendMessage({
          text,
          metadata: {
            sentAt: Date.now(),
            ...(opts?.displayText && {
              display: {
                kind: opts.displayKind ?? ('context' as const),
                label: opts.displayText,
                ...(opts.displayOutcome && { outcome: opts.displayOutcome }),
              },
            }),
            ...(opts?.authorization && { authorization: opts.authorization }),
            ...(opts?.files?.length && { attachments: opts.files }),
          },
        });
      };
      if (active && opts?.interrupt) {
        // Stop first, then a NEW turn — never a steer into the one being
        // cancelled. The abort finishes the session synchronously, so once
        // `stop` settles the transport has no active turn to reroute into.
        void stop().then(startTurn, startTurn);
        return;
      }
      if (active) {
        // Steering: emit into the RUNNING turn; the user bubble is inserted
        // BEFORE the streaming assistant message (which must stay last — the
        // AI SDK continues it by replace-last).
        const steered = engine.transport.steer(conversationId, text);
        if (steered) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            return last?.role === 'assistant'
              ? [...prev.slice(0, -1), steered.userMessage, last]
              : [...prev, steered.userMessage];
          });
        }
        return;
      }
      startTurn();
    },
    [conversationId, engine, isReadOnly, sendMessage, setMessages, status, stop],
  );

  // --- derived interrupt + workplan surfaces -----------------------------------
  const lastAssistant = useMemo(
    () => [...messages].reverse().find((m) => m.role === 'assistant'),
    [messages],
  );

  const interruptPending = useMemo<PendingApproval[]>(() => {
    if (!lastAssistant) return [];
    return lastAssistant.parts
      .filter(
        (p): p is Extract<typeof p, { type: 'dynamic-tool' }> =>
          p.type === 'dynamic-tool' &&
          p.state === 'approval-requested' &&
          // Awaited client-action requests are machine-answered (below), never
          // a human card — the risk-gate approval for the client_action TOOL
          // CALL (no marker) still surfaces normally.
          !(p.input as { _clientActionRequest?: boolean } | undefined)?._clientActionRequest,
      )
      .map((p) => ({
        toolCallId: p.toolCallId,
        toolName: p.toolName,
        input: (p.input ?? {}) as Record<string, unknown>,
        approvalId: (p as { approval?: { id: string } }).approval?.id ?? p.toolCallId,
      }));
  }, [lastAssistant]);

  // --- approval response ------------------------------------------------------
  // ONE pause for an approved `client_action`: the directive is already fully
  // described by the card's own args, so run it HERE, the moment the user
  // approves, and send the outcome on the decision. The agent then resumes once,
  // with the result in hand — instead of resuming to run the tool, interrupting
  // a second time for the browser's answer, and resuming again. Each of those
  // pauses is a full A2A resume, and the first also replays the model node.
  //
  // Every other path is untouched, and this one degrades safely: a directive we
  // cannot build, or a run that throws, falls through to a plain approve — the
  // tool then interrupts for the result and the round trip below handles it, as
  // it must anyway for an agent that predates this shortcut.
  const respond = useCallback(
    async (approvalId: string, approved: boolean, reason?: string) => {
      const pending = interruptPending.find((p) => p.approvalId === approvalId);
      if (approved && !reason && pending?.toolName === CLIENT_ACTION_TOOL) {
        const directive = directiveFromToolArgs(pending.input);
        if (directive) {
          try {
            // Approved with a click, by construction: this is the card's Approve.
            const result = await engine.core.runClientAction(directive, { approved: true });
            await addToolApprovalResponse({
              id: approvalId,
              ...encodeApproval({
                type: 'approve',
                client_action_result: result as unknown as Record<string, unknown>,
              }),
            });
            return;
          } catch {
            // Fall through to a plain approve — the tool asks for itself.
          }
        }
      }
      await addToolApprovalResponse({ id: approvalId, approved, ...(reason && { reason }) });
    },
    [addToolApprovalResponse, engine, interruptPending],
  );

  // --- client-action auto-settle (the awaited round trip) ----------------------
  // The paused `client_action` tool sent a directive and awaits its RESULT:
  // execute it against the host registry and answer through the same approval
  // machinery a human uses — `sendAutomaticallyWhen` then resumes the turn with
  // the result riding the decision envelope. The ref guards double-execution
  // (StrictMode double effects, re-renders while the response is in flight);
  // a reload re-arrives here via the restored-interrupt path and re-executes,
  // which is the wanted recovery.
  const settledActionsRef = useRef(new Set<string>());
  useEffect(() => {
    if (!lastAssistant || isReadOnly) return;
    for (const part of lastAssistant.parts) {
      if (part.type !== 'dynamic-tool' || part.state !== 'approval-requested') continue;
      const input = part.input as
        | { directive?: unknown; _clientActionRequest?: boolean }
        | undefined;
      if (!input?._clientActionRequest || !input.directive) continue;
      const approvalId = (part as { approval?: { id: string } }).approval?.id ?? part.toolCallId;
      if (settledActionsRef.current.has(approvalId)) continue;
      settledActionsRef.current.add(approvalId);
      void (async () => {
        // Right after a reload the restored request can run before the page has
        // registered its target (a form that mounts once its data is in): wait for
        // the registrations to settle rather than answer `unknown-target`.
        const target = (input.directive as { target?: { type?: unknown; id?: unknown } }).target;
        if (
          target &&
          typeof target.type === 'string' &&
          typeof target.id === 'string' &&
          !engine.core.registry.get(target.type, target.id)
        ) {
          await settleRegistrations(engine.core.registry);
        }
        // After a plain approve the tool asks for the result itself: the approval is the
        // card for the same call, answered in this message.
        const callId = wireCallId(approvalId);
        const approved = lastAssistant.parts.some(
          (p) =>
            p.type === 'dynamic-tool' &&
            p.toolCallId === callId &&
            (p as { approval?: { approved?: boolean } }).approval?.approved === true,
        );
        const result = await engine.core.runClientAction(replayableDirective(approvalId, input.directive), {
          approved,
        });
        await addToolApprovalResponse({
          id: approvalId,
          ...encodeApproval({
            type: 'approve',
            client_action_result: result as unknown as Record<string, unknown>,
          }),
        });
      })();
    }
  }, [lastAssistant, isReadOnly, engine, addToolApprovalResponse]);

  const workingSteps = useMemo<TodoItem[]>(() => {
    if (!lastAssistant || status === 'ready') {
      // Sticky behavior: the last turn's plan stays visible after completion.
      const source = lastAssistant ?? messages[messages.length - 1];
      const part = [...(source?.parts ?? [])].reverse().find((p) => p.type === 'data-workplan');
      return part && 'data' in part ? (part.data as { todos: TodoItem[] }).todos : [];
    }
    const part = [...lastAssistant.parts].reverse().find((p) => p.type === 'data-workplan');
    return part && 'data' in part ? (part.data as { todos: TodoItem[] }).todos : [];
  }, [lastAssistant, messages, status]);

  // --- pagination ---------------------------------------------------------------
  const loadingOlderRef = useRef(false);
  const loadOlderMessages = useCallback(async () => {
    if (loadingOlderRef.current || status !== 'ready') return;
    const page = engine.conversations.pageState(conversationId);
    if (!page.hasMore || !page.cursor) return;
    loadingOlderRef.current = true;
    try {
      const rows = await fetchMessagePage(engine.adapter.api.fetch, conversationId, page.cursor);
      if (!rows) {
        // A 400 on an older page retires the cursor permanently.
        engine.conversations.setPageState(conversationId, { cursor: null, hasMore: false });
        return;
      }
      engine.conversations.setPageState(conversationId, {
        cursor: rows.nextCursor,
        hasMore: !!rows.nextCursor,
      });
      if (rows.items.length > 0) {
        const older = rowsToUIMessages(rows.items, { olderPage: true });
        setMessages((prev) => {
          const known = new Set(
            prev.flatMap((m) => [m.id, m.metadata?.persistedMessageId].filter(Boolean) as string[]),
          );
          return [...older.filter((m) => !known.has(m.id)), ...prev];
        });
      }
    } finally {
      loadingOlderRef.current = false;
    }
  }, [conversationId, engine, setMessages, status]);

  return {
    conversationId,
    messages,
    status,
    error,
    send,
    stop,
    interrupt: {
      pending: interruptPending,
      reason: lastAssistant?.metadata?.hitl?.reason,
      reviewConfigs: lastAssistant?.metadata?.hitl?.reviewConfigs ?? [],
      respond,
    },
    workingSteps,
    isBusy: status === 'streaming' || status === 'submitted',
    isReadOnly,
    loadOlderMessages,
    hasOlderMessages: engine.conversations.pageState(conversationId).hasMore,
  };
}

async function fetchMessagePage(
  fetcher: (path: string, init?: RequestInit) => Promise<Response>,
  conversationId: string,
  cursor: string | null,
): Promise<{ items: RestMessageRow[]; nextCursor: string | null } | null> {
  const params = new URLSearchParams();
  params.set('limit', String(MESSAGE_PAGE_SIZE));
  if (cursor) params.set('before', cursor);
  const resp = await fetcher(`/api/v1/messages/${encodeURIComponent(conversationId)}?${params}`);
  if (!resp.ok) {
    // 404 = a brand-new conversation the server hasn't seen — an empty page.
    if (resp.status === 404) return { items: [], nextCursor: null };
    return null;
  }
  const data = (await resp.json()) as Record<string, unknown>;
  const items = Array.isArray(data.items)
    ? (data.items as RestMessageRow[])
    : Array.isArray(data.messages)
      ? (data.messages as RestMessageRow[])
      : [];
  return { items, nextCursor: (data.next_cursor as string | undefined) ?? null };
}
