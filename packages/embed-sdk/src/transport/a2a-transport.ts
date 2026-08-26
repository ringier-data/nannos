/**
 * `A2AChatTransport` — the bridge between the AI SDK's `ChatTransport`
 * contract and the Nannos A2A-over-socket.io protocol.
 *
 * One shared `TransportClient` socket feeds N per-conversation
 * `TurnSession` streams. Events route by `contextId` (≡ conversationId) or,
 * for sid-only emits (errors, steering acks — the backend sends those without
 * a contextId), by the send-message id recorded for EVERY emit. The demux is
 * pure; this class owns lifecycle: session registry, room subscriptions,
 * cancel wiring, and the snapshot-based resume protocol.
 */
import type { TransportClient } from '../core/client';
import type { AgentResponseData, ConversationSnapshotData } from '../core/wire';
import { generateUUID } from '../core/protocol';
import type {
  AnyUIMessageChunk,
  ChatRequestOptions,
  ChatTransport,
  NannosUIMessage,
  NannosUIMessageChunk,
} from './ai-types';
import {
  buildHitlResumePayload,
  buildNewTurnPayload,
  classifySend,
  textOfUserMessage,
  type SendContext,
} from './send-payload';
import { isClientActionPartId } from './approval-codec';
import { TurnSession, type TurnFinishReason, type TurnSessionInit } from './turn-session';
import { labelAgentEvent, type WireLog } from './wire-log';

export interface TurnLifecycleEvent {
  type: 'finished' | 'error' | 'approval-pending' | 'reconcile';
  conversationId: string;
  /** Trailing streamed text, for unread previews/toasts. */
  preview?: string;
}

/** The slice of TransportClient the chat transport uses (fakeable in tests). */
export type A2AClientLike = Pick<
  TransportClient,
  | 'onAgentResponse'
  | 'onConversationSnapshot'
  | 'subscribe'
  | 'sendMessage'
  | 'cancelTask'
  | 'subscribeConversation'
>;

export interface A2ATransportDeps {
  client: A2AClientLike;
  /** Resolves once the initialize_client handshake is done (false on timeout). */
  whenReady: () => Promise<boolean>;
  getSendContext: () => SendContext;
  onTurnEvent?: (e: TurnLifecycleEvent) => void;
  /** Snapshot wait budget for reconnectToStream (ms). */
  snapshotTimeoutMs?: number;
  /** Raw-traffic ring buffer for the dev-mode inspector. */
  wireLog?: WireLog;
}

const SNAPSHOT_TIMEOUT_MS = 5_000;

/** A closed single-error stream — what a send that can't reach the wire returns. */
function errorStream(messageId: string, errorText: string): ReadableStream<AnyUIMessageChunk> {
  return new ReadableStream<AnyUIMessageChunk>({
    start(controller) {
      controller.enqueue({ type: 'start', messageId });
      controller.enqueue({ type: 'error', errorText });
      controller.enqueue({ type: 'finish' });
      controller.close();
    },
  });
}

export class A2AChatTransport implements ChatTransport<NannosUIMessage> {
  private readonly sessions = new Map<string, TurnSession>();
  /** send-message id → conversationId, for sid-only emits. Every emit registers here. */
  private readonly pendingSendIds = new Map<string, string>();
  private readonly snapshotWaiters = new Map<string, Set<(snap: ConversationSnapshotData) => void>>();
  /**
   * Sessions opened by `reconnectToStream` that are still WAITING for their
   * snapshot. They have to be routable already (a pending-HITL replay arrives
   * ahead of the snapshot), but they are not an active turn yet: a send during
   * the wait must open a NEW turn, never steer into one. The window is real —
   * the backend rejects `subscribe_conversation` for a conversation it has
   * never persisted (a just-created one), so the probe burns its full timeout.
   */
  private readonly probes = new Set<TurnSession>();
  private readonly disposers: Array<() => void> = [];
  private destroyed = false;

  constructor(private readonly deps: A2ATransportDeps) {
    this.attach();
  }

  /**
   * (Re)subscribe to the client. Idempotent, and callable AFTER `destroy()`:
   * React runs mount → cleanup → mount over the SAME memoized engine
   * (StrictMode, Fast Refresh), and a one-way destroy left the panel with a
   * healthy socket ("connected") and a transport that answered every send with
   * the "Not connected to the assistant backend." error stream.
   */
  attach(): void {
    if (this.disposers.length > 0) return;
    this.destroyed = false;
    const { client } = this.deps;
    this.disposers.push(client.onAgentResponse((data) => this.handleAgentResponse(data)));
    this.disposers.push(client.onConversationSnapshot((snap) => this.handleSnapshot(snap)));
    this.disposers.push(
      client.subscribe((state) => {
        // Socket came back: rejoin the room of every open turn so the stream
        // resumes (room-targeted emits stop the moment we're out of the room).
        if (state.socketConnected) {
          for (const id of this.sessions.keys()) client.subscribeConversation(id);
        }
      }),
    );
  }

  /** A turn is STREAMING here (steerable). An unconfirmed resume probe is not. */
  hasActiveTurn(conversationId: string): boolean {
    const session = this.sessions.get(conversationId);
    return !!session && !this.probes.has(session);
  }

  // ---- ChatTransport -----------------------------------------------------

  async sendMessages(
    options: {
      trigger: 'submit-message' | 'regenerate-message';
      chatId: string;
      messageId: string | undefined;
      messages: NannosUIMessage[];
      abortSignal: AbortSignal | undefined;
    } & ChatRequestOptions,
  ): Promise<ReadableStream<AnyUIMessageChunk>> {
    const { chatId, messages, abortSignal } = options;
    const sendId = generateUUID();
    const assistantMessageId = generateUUID();

    const ready = await this.deps.whenReady();
    if (!ready || this.destroyed) {
      return errorStream(assistantMessageId, 'Not connected to the assistant backend.');
    }

    const classified = classifySend(messages);
    if (classified.kind === 'empty') {
      return errorStream(assistantMessageId, 'Nothing to send.');
    }

    const ctx = this.deps.getSendContext();
    const payload =
      classified.kind === 'new-turn'
        ? buildNewTurnPayload({
            messageId: sendId,
            conversationId: chatId,
            userMessage: classified.userMessage,
            ctx,
          })
        : buildHitlResumePayload({
            messageId: sendId,
            conversationId: chatId,
            decisions: classified.answered.map((a) => a.decision),
            ctx,
          });

    // A session still sitting here is either a turn that never closed cleanly
    // (the consumer dropped the stream without cancel) or a resume probe still
    // waiting for its snapshot. Tear it down SILENTLY — this send is the
    // outcome — so the new turn owns the routing slot.
    this.teardownSession(chatId);

    // A HITL resume CONTINUES the interrupted assistant message (the AI SDK
    // seeds its active message from it; a fresh start-id would push a
    // duplicate). The answered tool parts are settled with synthetic outputs —
    // approved → output-available, rejected/edited → output-denied — so
    // `sendAutomaticallyWhen` cannot re-fire on them, and `start-step` scopes
    // the resumed content as a new step (also the reset-step boundary).
    const session =
      classified.kind === 'new-turn'
        ? this.createSession(chatId, { startMessageId: assistantMessageId })
        : this.createSession(chatId, {
            startMessageId: null,
            // What this turn has already answered — a stale prompt replay of
            // the SAME kind for these calls is dropped instead of re-rendered.
            // The PART id says which kind was answered: only a client-action
            // request's part carries the suffix. It cannot be read off the
            // payload — an approved risk gate now also carries a result, since
            // the host runs the directive at approve-time.
            answeredApprovals: classified.answered
              .filter((a) => !!a.decision.id)
              .map((a) => ({
                id: a.decision.id!,
                kind: isClientActionPartId(a.partId) ? ('client-action' as const) : ('hitl' as const),
              })),
            initialChunks: [
              // Settle the part the answer actually came from, by its own id.
              ...classified.answered.map(({ partId, decision }): NannosUIMessageChunk =>
                decision.type === 'approve'
                  ? {
                      type: 'tool-output-available',
                      toolCallId: partId,
                      output: {
                        approved: true,
                        ...(decision.bypass && { bypass: true }),
                        // The settled part shows WHAT the browser actually did
                        // (applied / rejected fields), whichever path ran it.
                        ...(decision.client_action_result && {
                          result: decision.client_action_result,
                        }),
                      },
                      dynamic: true,
                    }
                  : { type: 'tool-output-denied', toolCallId: partId },
              ),
              { type: 'start-step' },
            ],
          });
    this.pendingSendIds.set(sendId, chatId);

    this.deps.wireLog?.push({
      dir: 'out',
      conversationId: chatId,
      label: classified.kind === 'new-turn' ? 'send-message' : 'hitl-resume',
      payload,
    });
    const sent = this.deps.client.sendMessage(payload);
    if (!sent) {
      this.teardownSession(chatId, sendId);
      return errorStream(assistantMessageId, 'Connection lost — message not sent.');
    }
    // Deliberately NO subscribe_conversation here. The backend joins the
    // conversation room inside its own send handler, before the turn starts
    // (console-backend app.py, handle_send_message), so a subscribe buys no
    // delivery — and it ANSWERS WITH A SNAPSHOT. On a HITL resume that snapshot
    // races the send handler's `_pending_interactions.pop`: it still carries the
    // prompt we are answering right now, the client replays `pendingHitl`
    // through agent_response, and the turn gets a duplicate approval card plus
    // an `input-required` that closes THIS stream — so the resumed answer
    // routes nowhere. `TurnSession.handle` drops such a replay by call id too.

    abortSignal?.addEventListener(
      'abort',
      () => {
        this.deps.client.cancelTask(chatId);
        // Close immediately; the room's late 'cancelled' echo finds no session.
        session.finish('abort');
      },
      { once: true },
    );

    return session.stream as ReadableStream<AnyUIMessageChunk>;
  }

  async reconnectToStream(options: {
    chatId: string;
  } & ChatRequestOptions): Promise<ReadableStream<AnyUIMessageChunk> | null> {
    const { chatId } = options;
    if (this.destroyed) return null;
    if (this.sessions.has(chatId)) {
      // A live session already owns this turn (background streaming) — the AI
      // SDK cannot re-attach to a partially consumed stream mid-flight.
      return null;
    }
    const ready = await this.deps.whenReady();
    if (!ready) return null;

    // Create the session BEFORE subscribing: the server replays a pending HITL
    // through agent_response ahead of the snapshot, and it must find a route.
    const session = this.createSession(chatId, { startMessageId: generateUUID() });
    this.probes.add(session);

    // The snapshot carries the resume payload (accumulated reply, pending HITL),
    // so it is still what we wait for — but the subscribe ACK answers in
    // milliseconds whether anything is coming at all. Without it, a conversation
    // the backend has never persisted (a just-created one, whose room join it
    // rejects outright) left this waiting for the full timeout.
    const snapshot = this.armSnapshot(chatId);
    let idle = false;
    this.deps.client.subscribeConversation(chatId, (ack) => {
      if (!ack) return; // unreadable ack — fall back to the snapshot timeout
      // A rejected join emits no snapshot at all; `!inFlight` means the backend
      // has nothing to resume (its snapshot, if any, would say the same).
      if (!ack.ok || !ack.inFlight) {
        idle = ack.ok;
        snapshot.cancel();
      }
    });

    const snap = await snapshot.promise;
    if (session.closed) {
      // 'destroyed' = a real send (or a teardown) claimed the routing slot
      // while we waited. That turn owns the conversation now — resume nothing,
      // or the AI SDK would attach this empty stream as an assistant message.
      if (session.finishReason === 'destroyed') return null;
      // The pendingHitl replay ran to input-required: the stream is complete
      // and already carries the approval parts — hand it over as-is.
      return session.stream as ReadableStream<AnyUIMessageChunk>;
    }
    if (!snap || !snap.inFlight) {
      this.teardownSession(chatId);
      // Reconcile only when the backend actually answered "no turn running":
      // the conversation may have finished its turn while we were away, so the
      // list needs a refreshed preview. A rejected join has nothing to reconcile.
      if (snap || idle) this.deps.onTurnEvent?.({ type: 'reconcile', conversationId: chatId });
      return null;
    }
    // Confirmed in flight: a live turn from here on, and steerable.
    this.probes.delete(session);
    session.seedFromSnapshot(snap.replyText ?? '', snap.offset ?? 0);
    return session.stream as ReadableStream<AnyUIMessageChunk>;
  }

  // ---- Steering (send while a turn is streaming) ---------------------------

  /**
   * Emit a follow-up into the RUNNING turn. Never goes through
   * `chat.sendMessage` (that would open a competing AI SDK response); the
   * backend reroutes the text to the live executor and acks `steering:true`,
   * which the demux swallows. Returns the user message for the caller to
   * append to UI state, or null when the send could not be emitted.
   */
  steer(conversationId: string, text: string): { userMessage: NannosUIMessage } | null {
    if (!this.hasActiveTurn(conversationId)) return null;
    const sendId = generateUUID();
    const userMessage: NannosUIMessage = {
      id: sendId,
      role: 'user',
      parts: [{ type: 'text', text }],
    };
    const payload = buildNewTurnPayload({
      messageId: sendId,
      conversationId,
      userMessage,
      ctx: this.deps.getSendContext(),
    });
    this.pendingSendIds.set(sendId, conversationId);
    this.deps.wireLog?.push({ dir: 'out', conversationId, label: 'steer', payload });
    if (!this.deps.client.sendMessage(payload)) {
      this.pendingSendIds.delete(sendId);
      return null;
    }
    return { userMessage };
  }

  /** Detach from the client and close every open turn. `attach()` revives it. */
  destroy(): void {
    this.destroyed = true;
    const open = [...this.sessions.values()];
    this.sessions.clear(); // deregister first → finish() below stays silent
    for (const session of open) session.finish('destroyed');
    this.probes.clear();
    this.pendingSendIds.clear();
    this.snapshotWaiters.clear();
    for (const dispose of this.disposers) dispose();
    this.disposers.length = 0;
  }

  /** Remove a session without lifecycle notification — the caller substitutes
   *  its own outcome (an error stream, or a null resume). */
  private teardownSession(conversationId: string, sendId?: string): void {
    const session = this.sessions.get(conversationId);
    this.sessions.delete(conversationId);
    if (sendId) this.pendingSendIds.delete(sendId);
    if (session && !session.closed) session.finish('destroyed');
  }

  // ---- Internals -----------------------------------------------------------

  private createSession(conversationId: string, init: TurnSessionInit): TurnSession {
    const session = new TurnSession(conversationId, init, {
      onCancel: () => this.deps.client.cancelTask(conversationId),
      onFinish: (reason) => this.onSessionFinished(conversationId, session, reason),
      onSteeringAck: () => {
        // Edge (post-reload send racing an unknown active turn): our "new turn"
        // was rerouted as steering into a run we haven't seen. If this session
        // has produced nothing beyond its start chunk, adopt the running turn:
        // re-subscribing yields a snapshot that seeds the head; live chunks
        // then continue into this same stream (routing is by conversationId).
        if (session.chunksEmitted <= 1) {
          this.deps.client.subscribeConversation(conversationId);
        }
      },
    });
    this.sessions.set(conversationId, session);
    return session;
  }

  private onSessionFinished(
    conversationId: string,
    session: TurnSession,
    reason: TurnFinishReason,
  ): void {
    const wasRegistered = this.sessions.get(conversationId) === session;
    if (wasRegistered) this.sessions.delete(conversationId);
    this.probes.delete(session);
    for (const [sendId, convId] of this.pendingSendIds) {
      if (convId === conversationId) this.pendingSendIds.delete(sendId);
    }
    if (!wasRegistered) return; // torn down silently (replaced send / destroy)
    const type =
      reason === 'error' ? 'error' : reason === 'input-required' ? 'approval-pending' : 'finished';
    this.deps.onTurnEvent?.({
      type,
      conversationId,
      preview: session.state.textBuffer.slice(0, 120) || undefined,
    });
  }

  private handleAgentResponse(data: AgentResponseData): void {
    const conversationId =
      data.contextId ?? (data.id ? this.pendingSendIds.get(data.id) : undefined);
    // Logged before routing, so even unroutable late echoes are inspectable.
    this.deps.wireLog?.push({
      dir: 'in',
      ...(conversationId && { conversationId }),
      label: labelAgentEvent(data),
      payload: data,
    });
    if (!conversationId) return;
    const session = this.sessions.get(conversationId);
    if (!session) return; // late echo after finish, or a turn another surface owns
    session.handle(data);
  }

  private handleSnapshot(snap: ConversationSnapshotData): void {
    const waiters = this.snapshotWaiters.get(snap.conversationId);
    if (waiters) {
      this.snapshotWaiters.delete(snap.conversationId);
      for (const resolve of waiters) resolve(snap);
      return;
    }
    // Unsolicited snapshot (socket reconnect rejoin): seed the live session's gap.
    const session = this.sessions.get(snap.conversationId);
    if (session && snap.inFlight) {
      session.seedFromSnapshot(snap.replyText ?? '', snap.offset ?? 0);
    }
  }

  /**
   * Arm a one-shot snapshot waiter. Arm it BEFORE emitting the subscribe: the
   * backend emits the snapshot ahead of the ack it returns for that same
   * subscribe, so a waiter armed afterwards can miss it.
   *
   * `cancel()` settles the wait with `null` immediately — for when the ack has
   * already told us no snapshot is coming. The timeout stays as the fallback
   * for a backend whose ack we cannot read.
   */
  private armSnapshot(conversationId: string): {
    promise: Promise<ConversationSnapshotData | null>;
    cancel: () => void;
  } {
    let settle: (snap: ConversationSnapshotData | null) => void = () => {};
    const promise = new Promise<ConversationSnapshotData | null>((resolve) => {
      const timeoutMs = this.deps.snapshotTimeoutMs ?? SNAPSHOT_TIMEOUT_MS;
      const timer = setTimeout(() => settle(null), timeoutMs);
      let settled = false;
      settle = (snap) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.snapshotWaiters.get(conversationId)?.delete(waiter);
        resolve(snap);
      };
      const waiter = (snap: ConversationSnapshotData) => settle(snap);
      let set = this.snapshotWaiters.get(conversationId);
      if (!set) {
        set = new Set();
        this.snapshotWaiters.set(conversationId, set);
      }
      set.add(waiter);
    });
    return { promise, cancel: () => settle(null) };
  }
}

export { textOfUserMessage };
export type { SendContext, NannosUIMessageChunk };
