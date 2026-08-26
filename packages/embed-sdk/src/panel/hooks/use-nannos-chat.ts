/**
 * The per-conversation chat facade: `useChat` over the scope's held `Chat`
 * instance, plus everything the old ChatContext did around it — history
 * seeding, keyset pagination, steering (send-while-streaming), the HITL
 * interrupt surface, and the seeded-prompt drain.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { generateUUID } from '../../core';
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
import { useConversations } from './use-conversations';

const MESSAGE_PAGE_SIZE = 100;

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
  /** Send, or STEER when a turn is already running (never interrupts it). */
  send: (
    text: string,
    opts?: {
      displayText?: string;
      files?: Array<{ uri: string; mimeType: string; name: string; s3Url?: string }>;
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
    // A keyed prompt about a DIFFERENT page context starts a fresh conversation.
    const target = engine.conversations.resolveTarget(seededPrompt.contextKey);
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
      engine.conversations.noteTitle(conversationId, opts?.displayText ?? text);
      void sendMessage({
        text,
        metadata: {
          ...(opts?.displayText && { display: { kind: 'context' as const, label: opts.displayText } }),
          ...(opts?.files?.length && { attachments: opts.files }),
        },
      });
    },
    [conversationId, engine, isReadOnly, sendMessage, setMessages, status],
  );

  const respond = useCallback(
    async (approvalId: string, approved: boolean, reason?: string) => {
      await addToolApprovalResponse({ id: approvalId, approved, ...(reason && { reason }) });
    },
    [addToolApprovalResponse],
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
