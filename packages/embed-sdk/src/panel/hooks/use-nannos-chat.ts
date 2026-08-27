/**
 * The per-conversation chat facade: `useChat` over the scope's held `Chat`
 * instance, plus everything the old ChatContext did around it — history
 * seeding, keyset pagination, steering (send-while-streaming), the HITL
 * interrupt surface, and the seeded-prompt drain.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { directiveFromToolArgs, generateUUID } from '../../core';
import { useChat } from '@ai-sdk/react';
import {
  appendRestoredInterrupt,
  encodeApproval,
  findPendingInterrupt,
  rowsToUIMessages,
  type NannosUIMessage,
  type ReviewConfig,
  type RestMessageRow,
  type TodoItem,
} from '../../transport';
import { useAssistant } from '../../react';
import { useChatEngine } from '../engine';
import { CLIENT_ACTION_TOOL, clientActionKind } from '../tool-title';
import { useApplyMode } from '../apply-mode';
import { useConversations } from './use-conversations';

const MESSAGE_PAGE_SIZE = 100;

/** Shared empty set — a fresh one per render would re-run every dependent memo. */
const EMPTY_IDS: ReadonlySet<string> = new Set();

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
  const applyMode = useApplyMode();
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
      if (rows.items.length > 0 && chat.messages.length === 0) {
        let mapped = rowsToUIMessages(rows.items);
        const interrupt = findPendingInterrupt(rows.items);
        if (interrupt) mapped = appendRestoredInterrupt(mapped, interrupt);
        chat.messages = mapped;
      }
      // Rejoin the stream room; an in-flight turn resumes via the snapshot.
      void chat.resumeStream();
    })();
    return () => {
      cancelled = true;
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
      ...(seededPrompt.displayText && {
        metadata: { display: { kind: 'context' as const, label: seededPrompt.displayText } },
      }),
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
        engine.conversations.noteTitle(conversationId, opts?.displayText ?? text);
        void sendMessage({
          text,
          metadata: {
            ...(opts?.displayText && { display: { kind: 'context' as const, label: opts.displayText } }),
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

  // --- apply mode: 'allow-edits' answers a form fill for the user -------------
  // Only a `client_action` of kind `apply` — the write into a registered form.
  // An unknown kind keeps its card: the agent scores those to interrupt as a
  // fail-safe, and honouring that is the point of the fail-safe.
  //
  // `failedAutoApply` is the escape hatch. These entries are hidden from the
  // card while the panel answers them, so a throw on the way out would hide a
  // pending approval forever and park the turn. On failure the id comes back
  // and the human sees the card, exactly as in manual mode.
  const [failedAutoApply, setFailedAutoApply] = useState<ReadonlySet<string>>(EMPTY_IDS);
  const autoApplyIds = useMemo<ReadonlySet<string>>(() => {
    if (applyMode !== 'allow-edits' || isReadOnly) return EMPTY_IDS;
    const ids = new Set<string>();
    for (const p of interruptPending) {
      if (p.toolName !== CLIENT_ACTION_TOOL) continue;
      if (clientActionKind(p.input) !== 'apply') continue;
      if (failedAutoApply.has(p.approvalId)) continue;
      ids.add(p.approvalId);
    }
    return ids;
  }, [applyMode, failedAutoApply, interruptPending, isReadOnly]);

  // What a HUMAN is asked about. The panel's own answers never render a card.
  const visiblePending = useMemo<PendingApproval[]>(
    () =>
      autoApplyIds.size === 0
        ? interruptPending
        : interruptPending.filter((p) => !autoApplyIds.has(p.approvalId)),
    [autoApplyIds, interruptPending],
  );

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
            const result = await engine.core.runClientAction(directive);
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

  // Fire the answers the mode implies. One attempt per approval id (the ref),
  // so a re-render while the response is in flight cannot double-apply. The
  // effect runs right after the render that hid the card, so nothing flashes.
  const autoAppliedRef = useRef(new Set<string>());
  useEffect(() => {
    for (const approvalId of autoApplyIds) {
      if (autoAppliedRef.current.has(approvalId)) continue;
      autoAppliedRef.current.add(approvalId);
      void respond(approvalId, true).catch(() => {
        // Could not answer for the user — give the approval back to them.
        setFailedAutoApply((prev) => new Set(prev).add(approvalId));
      });
    }
  }, [autoApplyIds, respond]);

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
        const result = await engine.core.runClientAction(input.directive);
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
        const older = rowsToUIMessages(rows.items);
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
      pending: visiblePending,
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
