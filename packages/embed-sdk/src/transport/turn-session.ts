/**
 * One agent turn = one `ReadableStream<UIMessageChunk>` handed to the AI SDK.
 * The session owns the stream controller, threads every routed
 * `agent_response` through the pure demux, and closes the stream on a
 * terminal status / error / abort. Snapshot seeding (reconnect/late
 * subscribe) reuses the same offset discipline as live chunks.
 */
import type { AgentResponseData } from '../core/wire';
import { generateUUID } from '../core/protocol';
import {
  approvalPrompt,
  createDemuxState,
  demux,
  type ApprovalPromptKind,
  type DemuxDone,
  type DemuxState,
} from './demux';
import { sliceFromCodePoint } from './offsets';
import { textArrival } from './ai-types';
import type { NannosUIMessageChunk } from './ai-types';

export type TurnFinishReason = DemuxDone | 'abort' | 'destroyed';

export interface TurnSessionInit {
  /**
   * Message id for the `start` chunk. `null` = CONTINUE the message the AI SDK
   * seeded from the last assistant message (the HITL-resume path: overriding
   * the id there would push a duplicate message instead of continuing).
   */
  startMessageId: string | null;
  /** Chunks enqueued right after `start` — e.g. the synthetic tool-output
   *  settles + `start-step` that open a HITL-resumed step. */
  initialChunks?: NannosUIMessageChunk[];
  /** The approvals this turn was opened by answering (the HITL-resume path),
   *  each with the KIND of prompt that was answered. A prompt of the SAME kind
   *  re-asking only these is a stale replay — see `handle`. */
  answeredApprovals?: Array<{ id: string; kind: ApprovalPromptKind }>;
}

export class TurnSession {
  readonly stream: ReadableStream<NannosUIMessageChunk>;
  /** Part-id prefix is per-turn: a resumed turn writes into the SAME message
   *  as the interrupted one, so generated ids must never collide across turns. */
  readonly state: DemuxState = createDemuxState(`${generateUUID().slice(0, 8)}-`);
  /** True once the stream is closed; late room echoes then route nowhere. */
  closed = false;
  /** How the turn ended (set exactly once, by finish()). */
  finishReason: TurnFinishReason | null = null;
  /** Count of chunks enqueued — a session that received nothing yet can adopt
   *  a running turn when its own send was acked as steering. */
  chunksEmitted = 0;

  private controller: ReadableStreamDefaultController<NannosUIMessageChunk> | null = null;
  /** Approval calls this turn has already answered, by prompt kind (empty for
   *  a new turn). Keyed by kind because one call id can be prompted TWICE with
   *  different kinds — see `approvalPrompt`. */
  private readonly answered: ReadonlyMap<string, ApprovalPromptKind>;

  constructor(
    readonly conversationId: string,
    init: TurnSessionInit,
    private readonly hooks: {
      /** The consumer cancelled the stream (AI SDK stop/teardown). */
      onCancel?: () => void;
      onFinish?: (reason: TurnFinishReason) => void;
      /** The backend acked one of this session's sends as steering. */
      onSteeringAck?: () => void;
    } = {},
  ) {
    this.answered = new Map((init.answeredApprovals ?? []).map((a) => [a.id, a.kind]));
    this.stream = new ReadableStream<NannosUIMessageChunk>({
      start: (controller) => {
        this.controller = controller;
      },
      cancel: () => {
        this.hooks.onCancel?.();
        this.finish('abort');
      },
    });
    this.emit(
      init.startMessageId != null
        ? { type: 'start', messageId: init.startMessageId }
        : { type: 'start' },
    );
    for (const chunk of init.initialChunks ?? []) this.emit(chunk);
  }

  private emit(chunk: NannosUIMessageChunk): void {
    if (this.closed || !this.controller) return;
    this.controller.enqueue(chunk);
    this.chunksEmitted += 1;
  }

  /** Route one `agent_response` event through the demux into the stream. */
  handle(data: AgentResponseData): void {
    if (this.closed) return;
    // Stale prompt replay: a `pendingHitl` snapshot (a subscribe racing this
    // resume, or a socket rejoin) re-asks a call this turn already answered.
    // Rendering it would duplicate the approval card AND close this stream on
    // `input-required`, dropping the resumed answer on the floor. A prompt
    // with any unanswered call in it still renders — and so does a prompt of a
    // DIFFERENT kind for the same call: an approved `client_action` tool emits
    // its round-trip request under the very `_call_id` just approved.
    if (this.answered.size > 0) {
      const asked = approvalPrompt(data);
      if (asked && asked.ids.every((id) => this.answered.get(id) === asked.kind)) return;
    }
    const result = demux(this.state, data);
    if (result.steering) {
      this.hooks.onSteeringAck?.();
      return;
    }
    for (const chunk of result.chunks) this.emit(chunk);
    if (result.done) this.finish(result.done);
  }

  /**
   * Seed from a resume snapshot: emit the accumulated head not yet applied.
   * Also used on socket reconnect for an already-live session — only the gap
   * beyond `appliedOffset` is emitted, so overlap never double-appends.
   * Offsets are CODE POINTS (server numbers), never JS `.length`.
   */
  seedFromSnapshot(replyText: string, offset: number): void {
    if (this.closed) return;
    if (offset <= this.state.appliedOffset) return;
    const gap = sliceFromCodePoint(replyText, this.state.appliedOffset);
    if (gap) {
      if (!this.state.textId) {
        this.state.textSeq += 1;
        this.state.textId = `txt-${this.state.textSeq}`;
        this.emit({
          type: 'text-start',
          id: this.state.textId,
          providerMetadata: textArrival(Date.now()),
        });
      }
      this.emit({ type: 'text-delta', id: this.state.textId, delta: gap });
      this.state.textBuffer += gap;
    }
    this.state.appliedOffset = offset;
  }

  /** Close the stream. Idempotent; flushes part closers before the finish chunk. */
  finish(reason: TurnFinishReason): void {
    if (this.closed) return;
    const closers: NannosUIMessageChunk[] = [];
    // Flush open parts so the final message is well-formed even on abort.
    if (this.state.openThought) {
      const t = this.state.openThought;
      closers.push({
        type: 'data-agent-thought',
        id: t.partId,
        data: { agent: t.agent, text: t.text, complete: true, startedAt: t.startedAt },
      });
      this.state.openThought = null;
    }
    if (this.state.textId) {
      closers.push({ type: 'text-end', id: this.state.textId });
      this.state.textId = null;
    }
    for (const chunk of closers) this.emit(chunk);
    this.emit({ type: 'finish' });
    this.closed = true;
    this.finishReason = reason;
    try {
      this.controller?.close();
    } catch {
      // Already closed/errored by the consumer — nothing to release.
    }
    this.hooks.onFinish?.(reason);
  }
}
