import { WebClient } from '@slack/web-api';
import type { ChatStreamer } from '@slack/web-api';
import type { AnyChunk, TaskUpdateChunk, AnyBlock } from '@slack/types';
import { Logger } from './logger.js';
import { postOrUpdateMessage, decisionRichText } from './taskResponseHandler.js';
import { getSpinnerVerb } from './spinnerVerbs.js';

const logger = Logger.getLogger('thinkingStepsStreamer');

// Slack caps task_update / plan_update titles at 256 chars. Card body fields
// (details/output) tolerate more, but we cap defensively to avoid platform errors.
const TITLE_MAX = 256;
const BODY_MAX = 2800;
// Step shown in the thinking widget while the final answer message is produced.
const GENERATING_TITLE = 'Generating response…';

/**
 * Work-plan todo shape carried on the `work-plan:1.0` A2A extension.
 */
export interface WorkPlanTodo {
  name: string;
  state: 'submitted' | 'working' | 'completed' | 'failed';
  source?: string;
  target?: string;
}

export interface ThinkingStepsStreamerOptions {
  channelId: string;
  threadTs: string;
  /** Team of the recipient — required by chat.startStream outside DMs. */
  teamId: string;
  /** User who will receive the stream — required by chat.startStream outside DMs. */
  userId: string;
  /** Initial plan-block title shown while the agent works. */
  initialTitle?: string;
  /**
   * Slack ts of an existing plan message to keep updating (carried across a HITL
   * resume), so the one plan widget is updated in place rather than re-posted.
   */
  planMessageTs?: string;
  /**
   * Slack ts of an existing OPEN thinking-steps stream to continue (carried across
   * a HITL resume). When set, the streamer appends to that message instead of
   * starting a new one — so a HITL flow stays in one thinking widget. The
   * orchestrator replays the trailing steps on resume; those append as new cards.
   */
  resumeStreamTs?: string;
}

const TODO_STATUS: Record<WorkPlanTodo['state'], TaskUpdateChunk['status']> = {
  submitted: 'pending',
  working: 'in_progress',
  completed: 'complete',
  failed: 'error',
};

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

/**
 * The reasoning text to DISPLAY given the raw buffer so far. Sub-agent
 * intermediate output is sometimes a structured-response envelope
 * (`{"task_state":"completed","message":"…"}`). When the buffer looks like such
 * an object, return the `message` value decoded SO FAR (supporting partial JSON
 * mid-stream); otherwise return the raw prose. Lets us stream $.message live.
 */
function reasoningDisplaySoFar(raw: string): string {
  if (raw.trimStart().startsWith('{')) {
    return extractJsonStringValueSoFar(raw, 'message') ?? '';
  }
  return raw;
}

/**
 * Extract the (possibly incomplete) value of a top-level string field from a JSON
 * buffer that may still be streaming. Returns the decoded characters available so
 * far (stopping before any partial escape), or null if the field's value hasn't
 * started yet. Handles \" \\ \/ \n \r \t \b \f and \uXXXX escapes.
 */
function extractJsonStringValueSoFar(raw: string, field: string): string | null {
  const keyIdx = raw.indexOf(`"${field}"`);
  if (keyIdx === -1) return null;
  let i = raw.indexOf(':', keyIdx + field.length + 2);
  if (i === -1) return null;
  i++;
  while (i < raw.length && /\s/.test(raw[i])) i++;
  if (i >= raw.length || raw[i] !== '"') return null;
  i++; // past the opening quote
  let out = '';
  while (i < raw.length) {
    const ch = raw[i];
    if (ch === '"') return out; // closing quote → value complete
    if (ch === '\\') {
      const next = raw[i + 1];
      if (next === undefined) return out; // partial escape at buffer end — stop
      switch (next) {
        case 'n': out += '\n'; break;
        case 't': out += '\t'; break;
        case 'r': out += '\r'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'u': {
          const hex = raw.slice(i + 2, i + 6);
          if (hex.length < 4) return out; // partial \u escape — stop
          out += String.fromCharCode(parseInt(hex, 16) || 0);
          i += 6;
          continue;
        }
        default:
          out += next; // \" \\ \/ and anything else → the literal char
      }
      i += 2;
      continue;
    }
    out += ch;
    i++;
  }
  return out; // ran out of buffer mid-value (still streaming)
}

/**
 * Final card status for a reasoning buffer: `error` if the structured output's
 * `task_state` indicates failure, otherwise `complete`.
 */
function reasoningFinalStatus(raw: string): TaskUpdateChunk['status'] {
  const t = raw.trim();
  if (t.startsWith('{') && t.endsWith('}')) {
    try {
      const state = JSON.parse(t)?.task_state;
      if (typeof state === 'string' && ['failed', 'error', 'rejected', 'canceled'].includes(state)) {
        return 'error';
      }
    } catch {
      /* ignore */
    }
  }
  return 'complete';
}

/**
 * Translates A2A status/artifact events into a single Slack "Thinking Steps"
 * streaming message (chat.startStream / appendStream / stopStream):
 *
 *   - work-plan todos    → plan_update title + task_update cards (state-tracked)
 *   - activity-log       → task_update cards (one per discrete action)
 *   - intermediate-output→ task_update cards ("💭 reasoning") — collapsible,
 *                          kept OUT of the visible answer body
 *   - final answer       → the ONLY markdown_text (the visible response body)
 *
 * task_update / plan_update chunks render in Slack's collapse-by-default
 * thinking-steps region; markdown_text renders as the answer. Keeping thinking
 * in cards makes it visually distinct from the answer and immune to body
 * duplication when a sub-agent draft ≈ the final answer.
 *
 * If chat streaming is unavailable (older workspace / missing scope), the
 * streamer transparently degrades to the legacy single-message status + final
 * post flow so replies keep working.
 */
export class ThinkingStepsStreamer {
  private streamer: ChatStreamer | undefined;
  private messageTs: string | undefined;
  // When continuing an existing open stream across a HITL resume, this is its ts
  // and we append to it via the raw chat.appendStream API (ChatStreamer can't
  // adopt an existing stream). Cleared if that stream has expired (then we start
  // a fresh one). Per-instance idPrefix keeps this turn's card ids distinct from
  // the previous turn's, so replayed steps append as new cards rather than
  // colliding with (overwriting) the earlier ones.
  private resumeStreamTs: string | undefined;
  private readonly idPrefix: string;
  // The final answer streams into its OWN new message (so its creation fires a
  // push notification — edits to the thinking widget don't). Meanwhile a
  // "Generating…" step shows in the thinking widget, completed when the answer
  // message is finalized.
  private answerStreamer: ChatStreamer | undefined;
  private answerStreamTs: string | undefined;
  private generatingCardId: string | undefined;
  // The dedicated todos/plan message (separate from the streamed timeline),
  // updated in place. Undefined until the first work-plan event.
  private planMessageTs: string | undefined;
  private planBlocksUnsupported = false;
  private planTitle: string;
  private answerStarted = false;
  private finished = false;
  private degraded = false;

  // Stable ids for task cards we render.
  private activityCounter = 0;
  // The currently-running activity card (rolling spinner); completed when the
  // next activity arrives or when the stream is finalized.
  private currentActivity: { id: string; title: string } | undefined;
  private readonly thinkingCardIds = new Map<string, string>();
  // Per-agent reasoning streaming state: the raw buffer (all deltas) and how many
  // chars of the DISPLAY text (the extracted $.message, or raw prose) we've
  // already streamed into the card's details.
  private readonly thinkingRaw = new Map<string, string>();
  private readonly thinkingEmitted = new Map<string, number>();
  // Whether the seeded bootstrap placeholder has been adopted by the first real
  // activity (it's reused once, then activities roll into their own cards).
  private bootstrapReused = false;
  // Track every card we own so we can flip in-progress → complete at the end.
  private readonly cardStatus = new Map<string, TaskUpdateChunk['status']>();

  // The full answer text streamed into the visible body so far. Used to diff
  // incoming answer snapshots (see appendAnswer) so the SAME answer arriving via
  // multiple channels — live artifact-append chunks plus the authoritative
  // terminal status message — doesn't double the body.
  private answerText = '';

  // ---- degraded-mode (no streaming) state ----
  private fallbackTs: string | undefined;
  private fallbackTodos = '';
  private fallbackActivity = '';
  private fallbackAnswer = '';

  constructor(
    private readonly client: WebClient,
    private readonly opts: ThinkingStepsStreamerOptions
  ) {
    this.planTitle = opts.initialTitle || 'Working';
    this.planMessageTs = opts.planMessageTs;
    this.resumeStreamTs = opts.resumeStreamTs;
    if (this.resumeStreamTs) this.messageTs = this.resumeStreamTs;
    // Unique per turn so this turn's card ids don't collide with the previous
    // turn's on the same (resumed) message.
    this.idPrefix = Math.random().toString(36).slice(2, 8);
  }

  /** Id of the seeded placeholder card (prefixed per turn). */
  private get bootstrapId(): string {
    return `${this.idPrefix}:act:boot`;
  }

  /** Slack ts of the plan message (if posted), so callers can carry it across a HITL resume. */
  get planTs(): string | undefined {
    return this.planMessageTs;
  }

  /** Slack ts of the streamed (or degraded-mode) message; undefined until started. */
  get ts(): string | undefined {
    return this.messageTs ?? this.fallbackTs;
  }

  /** True once final-answer text has been appended (so finalize won't double-post it). */
  get hasAnswer(): boolean {
    return this.answerStarted;
  }

  /** ts of the final-answer message (the new message users get notified about), if posted. */
  get answerTs(): string | undefined {
    return this.answerStreamTs;
  }

  /**
   * Start the stream eagerly so the user sees immediate responsiveness and we
   * capture a message ts for the in-flight task store. Safe to call once.
   */
  async start(): Promise<void> {
    if (this.streamer || this.degraded || this.finished) return;
    if (this.resumeStreamTs) {
      // Continuing an existing open stream across a HITL resume: re-activate its
      // plan title; no placeholder (the message already has the prior steps).
      await this.streamAppend({ chunks: [{ type: 'plan_update', title: truncate(this.planTitle, TITLE_MAX) }] });
      return;
    }
    // Seed an in-progress placeholder card with a whimsical verb (like the Slack
    // blog) so the message shows a spinning step immediately — a plan_update
    // title alone renders empty until the first real task card arrives. No emoji:
    // the native in_progress spinner is the indicator. The first real activity
    // REUSES this card in place (applyActivity) so it doesn't linger as clutter.
    const placeholderTitle = `${getSpinnerVerb() || 'Working'}…`;
    this.cardStatus.set(this.bootstrapId, 'in_progress');
    this.currentActivity = { id: this.bootstrapId, title: placeholderTitle };
    await this.streamAppend({
      chunks: [
        { type: 'plan_update', title: truncate(this.planTitle, TITLE_MAX) },
        { type: 'task_update', id: this.bootstrapId, title: placeholderTitle, status: 'in_progress' },
      ],
    });
    if (this.degraded) {
      // Streaming unavailable — fall back to an immediate plain status message.
      this.fallbackActivity = placeholderTitle;
      await this.renderFallbackStatus();
    }
  }

  /**
   * work-plan:1.0 → a single `plan` block (a checklist widget) in its OWN message,
   * updated in place. It can't share the streamed message: Slack treats a plan
   * block and task cards as mutually exclusive (and that combination breaks the
   * chat.update used to seal the HITL widget). So we post one dedicated plan
   * message and chat.update it as todos change — "always update the one widget".
   * Falls back to a plain-text checklist if plan blocks aren't supported.
   */
  async applyWorkPlan(todos: WorkPlanTodo[]): Promise<void> {
    if (!todos || todos.length === 0) return;
    const checklist = this.planChecklistText(todos);
    if (!this.planBlocksUnsupported) {
      try {
        const blocks = [this.buildPlanBlock(todos)];
        if (!this.planMessageTs) {
          const res = await this.client.chat.postMessage({
            channel: this.opts.channelId,
            thread_ts: this.opts.threadTs,
            text: 'Plan',
            blocks,
          });
          this.planMessageTs = res.ts as string | undefined;
        } else {
          await this.client.chat.update({ channel: this.opts.channelId, ts: this.planMessageTs, blocks });
        }
        return;
      } catch (err) {
        this.planBlocksUnsupported = true;
        logger.warn(`plan block unsupported, falling back to a text checklist: ${err instanceof Error ? err.message : err}`);
      }
    }
    // Plain-text checklist fallback (also covers degraded/no-streaming workspaces).
    this.planMessageTs = await postOrUpdateMessage(
      this.client,
      this.opts.channelId,
      this.opts.threadTs,
      checklist,
      this.planMessageTs
    );
  }

  /** ✓/⏳/🔜 checklist rendering of todos for the plain-text plan fallback. */
  private planChecklistText(todos: WorkPlanTodo[]): string {
    return (
      '*Plan*\n' +
      todos
        .map(
          (t) =>
            `${t.state === 'completed' ? '✅' : t.state === 'working' ? '⏳' : t.state === 'failed' ? '❌' : '🔜'} ${t.name}${t.source ? ` _(agent ${t.source})_` : ''}${t.target ? ` \`${t.target}\`` : ''}`
        )
        .join('\n')
    );
  }

  /**
   * activity-log:1.0 → a rolling task card. The newest action is shown as
   * `in_progress` (Slack animates a spinner on it while the stream is open) and
   * the previous action flips to `complete`, producing the live step-by-step
   * effect from Slack's docs. The final action is completed on finish().
   */
  async applyActivity(text: string, source?: string): Promise<void> {
    const clean = (text || '').trim();
    if (!clean) return;
    // Prefix the agent name into the TITLE rather than using `details`: a single
    // activity step doesn't warrant the collapse chevron that a details field adds.
    const title = truncate(source ? `${source}: ${clean}` : clean, TITLE_MAX);
    const chunks: AnyChunk[] = [];

    let id: string;
    if (
      !this.bootstrapReused &&
      this.currentActivity?.id === this.bootstrapId &&
      this.cardStatus.get(this.bootstrapId) === 'in_progress'
    ) {
      // First real activity ONLY → reuse the seeded placeholder card in place so
      // it doesn't linger as a separate "Working…" step. Subsequent activities
      // roll into their own cards (a timeline), never overriding this one again.
      id = this.bootstrapId;
      this.bootstrapReused = true;
    } else {
      // Flip the previous still-running step to complete, then open a new one.
      if (this.currentActivity && this.cardStatus.get(this.currentActivity.id) === 'in_progress') {
        this.cardStatus.set(this.currentActivity.id, 'complete');
        chunks.push({
          type: 'task_update',
          id: this.currentActivity.id,
          title: this.currentActivity.title,
          status: 'complete',
        });
      }
      id = `${this.idPrefix}:act:${this.activityCounter++}`;
    }

    this.cardStatus.set(id, 'in_progress');
    chunks.push({
      type: 'task_update',
      id,
      title,
      status: 'in_progress',
    });
    this.currentActivity = { id, title };

    await this.streamAppend({ chunks });
    if (this.degraded) {
      this.fallbackActivity = clean;
      await this.renderFallbackStatus();
    }
  }

  /**
   * intermediate-output:1.0 → a collapsible "reasoning" task card kept OUT of the
   * answer body. Reasoning streams LIVE into the card's `details` (which appends).
   *
   * To "always show text" even when a sub-agent's intermediate output is a
   * structured envelope (`{"task_state":…,"message":"…"}`), we stream only the
   * incrementally-extracted `$.message` value — the JSON scaffolding never shows.
   * Plain-prose reasoning streams through verbatim.
   */
  async appendThinking(text: string, agent?: string): Promise<void> {
    if (!text) return;
    const key = agent || 'agent';
    let id = this.thinkingCardIds.get(key);
    const isNew = !id;
    if (!id) {
      id = `${this.idPrefix}:think:${key}`;
      this.thinkingCardIds.set(key, id);
    }

    const raw = (this.thinkingRaw.get(key) || '') + text;
    this.thinkingRaw.set(key, raw);
    const display = reasoningDisplaySoFar(raw);
    const emitted = this.thinkingEmitted.get(key) || 0;
    const delta = display.length > emitted ? display.slice(emitted) : '';
    this.thinkingEmitted.set(key, display.length);

    this.cardStatus.set(id, 'in_progress');
    // Create the card on the first chunk (so the spinner shows even before any
    // displayable text), then append display deltas. `details` append-streams,
    // so each call carries only the new $.message (or prose) characters.
    if (isNew || delta) {
      await this.streamAppend({
        chunks: [
          {
            type: 'task_update',
            id,
            title: this.reasoningTitle(key),
            status: 'in_progress',
            ...(delta ? { details: delta } : {}),
          } satisfies TaskUpdateChunk,
        ],
      });
    }
    // Thinking text is intentionally NOT mirrored into the degraded-mode body
    // (the legacy flow also dropped it), keeping the answer clean.
  }

  /** Title for a reasoning card. No emoji — the native spinner/check is the icon. */
  private reasoningTitle(key: string): string {
    return truncate(key && key !== 'agent' ? `Reasoning · ${key}` : 'Reasoning', TITLE_MAX);
  }

  /**
   * Final answer chunk(s) → the only markdown_text (visible body).
   *
   * The same answer reaches us through two channels: live artifact-append
   * chunks streamed as the agent works, and the authoritative full answer in
   * the terminal status message (the server's `final_answer_source: "fallback"`
   * contract). Pass `snapshot: true` for the latter (and for any append=false
   * create chunk) so it is diffed against what we've already shown and only the
   * not-yet-streamed suffix is appended — deduping the body in the common case,
   * and topping up the answer if a live SSE frame was dropped. Incremental
   * (append=true) deltas pass `snapshot: false` and are appended verbatim.
   */
  async appendAnswer(text: string, snapshot = false): Promise<void> {
    if (!text) return;
    const delta = this.answerDelta(text, snapshot);
    if (!delta) {
      // Fully deduped (e.g. terminal answer == already-streamed body): nothing
      // new to render, but the answer phase HAS begun — record it so finalize
      // and the dangling-stream cleanup treat the turn as answered.
      this.answerStarted = true;
      return;
    }
    if (!this.answerStarted) {
      this.answerStarted = true;
      // Steps are done → complete them and show a "Generating response" step in
      // the thinking widget while the answer is produced in its OWN message.
      const completion = this.buildCompletionChunks();
      this.generatingCardId = `${this.idPrefix}:generating`;
      this.cardStatus.set(this.generatingCardId, 'in_progress');
      completion.push({ type: 'task_update', id: this.generatingCardId, title: GENERATING_TITLE, status: 'in_progress' });
      await this.streamAppend({ chunks: completion });
    }
    this.answerText += delta;
    if (this.degraded) {
      this.fallbackAnswer += delta;
      return;
    }
    await this.appendToAnswerStream(delta);
  }

  /**
   * Stream the answer into a SEPARATE new message (so its creation notifies the
   * user about completion — appends to the thinking widget wouldn't). Falls back
   * to the thinking widget body if a separate stream can't be opened.
   */
  private async appendToAnswerStream(text: string): Promise<void> {
    if (this.degraded || this.finished) return;
    try {
      if (!this.answerStreamer) {
        this.answerStreamer = this.client.chatStream({
          channel: this.opts.channelId,
          thread_ts: this.opts.threadTs,
          recipient_team_id: this.opts.teamId,
          recipient_user_id: this.opts.userId,
        });
      }
      const res = await this.answerStreamer.append({ markdown_text: text });
      if (res && !this.answerStreamTs && 'ts' in res && res.ts) this.answerStreamTs = res.ts;
    } catch (err) {
      logger.warn(`answer stream unavailable, writing answer into the thinking widget: ${err instanceof Error ? err.message : err}`);
      this.answerStreamer = undefined;
      await this.streamAppend({ markdown_text: text });
    }
  }

  /**
   * Portion of an answer chunk not already streamed into the body. Incremental
   * deltas (append=true) are returned verbatim; snapshots — a cumulative
   * create chunk, the terminal status message, or a full answer re-sent through
   * a second channel — are diffed: an exact resend yields nothing, a superset
   * yields just the new suffix.
   */
  private answerDelta(text: string, snapshot: boolean): string {
    if (!snapshot || !this.answerText) return text;
    if (text === this.answerText) return '';
    if (text.startsWith(this.answerText)) return text.slice(this.answerText.length);
    if (this.answerText.includes(text)) return '';
    return text;
  }

  /**
   * Finalize the message. Optionally appends a trailing markdown block (e.g. file
   * links) and concluding blocks (e.g. the feedback widget). Idempotent.
   */
  async finish(opts?: { trailingMarkdown?: string; blocks?: AnyBlock[]; planTitle?: string }): Promise<void> {
    if (this.finished) return;
    // Build the "flip remaining in-progress steps → complete" chunks BEFORE
    // marking finished (so they ride along in the stop() call below — flushChunks
    // no-ops once finished is set).
    const completion = this.buildCompletionChunks();
    // Complete the "Generating response" step (if shown).
    if (this.generatingCardId && this.cardStatus.get(this.generatingCardId) === 'in_progress') {
      this.cardStatus.set(this.generatingCardId, 'complete');
      completion.push({ type: 'task_update', id: this.generatingCardId, title: GENERATING_TITLE, status: 'complete' });
    }
    this.finished = true;

    if (this.degraded) {
      // No stream to attach blocks to — post the widget (e.g. HITL approval) as
      // its own message so the interaction still works without streaming.
      if (opts?.blocks && opts.blocks.length > 0) {
        await this.client.chat
          .postMessage({
            channel: this.opts.channelId,
            thread_ts: this.opts.threadTs,
            text: '⚠️ Approval Required',
            blocks: opts.blocks,
          })
          .catch((err) => logger.error(err, `Failed to post widget in degraded mode: ${err}`));
      }
      const body = `${this.fallbackAnswer}${opts?.trailingMarkdown ?? ''}`.trim();
      if (body) {
        this.fallbackTs = await postOrUpdateMessage(
          this.client,
          this.opts.channelId,
          this.opts.threadTs,
          body,
          this.fallbackTs
        );
      } else if (this.fallbackTs) {
        // Nothing to say — remove the leftover status message.
        await this.client.chat
          .delete({ channel: this.opts.channelId, ts: this.fallbackTs })
          .catch(() => {});
        this.fallbackTs = undefined;
      }
      return;
    }

    // 1. Finalize the SEPARATE answer message (the final response → notification).
    //    Trailing markdown (file links) goes here, with the answer.
    if (this.answerStreamer) {
      try {
        await this.answerStreamer.stop(opts?.trailingMarkdown ? { markdown_text: opts.trailingMarkdown } : {});
      } catch (err) {
        logger.error(err, `Failed to stop answer stream: ${err}`);
      }
    } else if (opts?.trailingMarkdown) {
      // File links but no streamed answer text — post them as their own message.
      this.answerStreamTs = (
        await this.client.chat
          .postMessage({ channel: this.opts.channelId, thread_ts: this.opts.threadTs, markdown_text: opts.trailingMarkdown })
          .catch((err) => {
            logger.error(err, `Failed to post trailing answer: ${err}`);
            return undefined;
          })
      )?.ts as string | undefined;
    }

    // 2. Finalize the thinking widget (complete steps + settle the plan title).
    if (!this.streamer && !this.resumeStreamTs) return;
    try {
      const chunks: AnyChunk[] = [...completion];
      // Re-label the (collapsed) plan disclosure — e.g. "Awaiting your approval"
      // when sealing the trace at a HITL interrupt rather than at completion.
      if (opts?.planTitle) chunks.push({ type: 'plan_update', title: truncate(opts.planTitle, TITLE_MAX) });
      const stopArgs: { blocks?: AnyBlock[]; chunks?: AnyChunk[] } = {};
      if (opts?.blocks && opts.blocks.length > 0) stopArgs.blocks = opts.blocks;
      if (chunks.length > 0) stopArgs.chunks = chunks;
      if (this.streamer) {
        await this.streamer.stop(stopArgs);
      } else {
        await this.client.chat.stopStream({ channel: this.opts.channelId, ts: this.resumeStreamTs!, ...stopArgs });
      }
    } catch (err) {
      logger.error(err, `Failed to stop thinking-steps stream: ${err}`);
    }
  }

  /**
   * Seal the stream for a HITL interrupt WITHOUT stopping it: complete the
   * in-progress steps and relabel the plan, but leave the stream open so the
   * resume turn can continue it (carry `ts` via {@link planTs}/{@link ts}).
   */
  async pause(planTitle: string): Promise<void> {
    if (this.finished || this.degraded) return;
    const completion = this.buildCompletionChunks();
    await this.streamAppend({
      chunks: [...completion, { type: 'plan_update', title: truncate(planTitle, TITLE_MAX) }],
    });
  }

  /**
   * Discard the message entirely (used on early-return paths that post their own
   * reply, e.g. auth prompts / debug commands). Stops any open stream, then
   * deletes the message so no empty "Working…" card is left behind — the
   * streaming equivalent of the legacy "delete the thinking message" cleanup.
   */
  async discard(): Promise<void> {
    if (!this.finished) {
      this.finished = true;
      if (this.streamer) {
        await this.streamer.stop().catch(() => {});
      } else if (this.resumeStreamTs) {
        await this.client.chat
          .stopStream({ channel: this.opts.channelId, ts: this.resumeStreamTs })
          .catch(() => {});
      }
      if (this.answerStreamer) await this.answerStreamer.stop().catch(() => {});
    }
    const ts = this.ts;
    if (ts) {
      await this.client.chat
        .delete({ channel: this.opts.channelId, ts })
        .catch((err) => logger.trace(err, `Failed to delete streamed message: ${err}`));
    }
    // Also remove the separate plan message, if one was created.
    if (this.planMessageTs) {
      await this.client.chat
        .delete({ channel: this.opts.channelId, ts: this.planMessageTs })
        .catch(() => {});
      this.planMessageTs = undefined;
    }
  }

  // -------------------------------------------------------------------------
  // internals
  // -------------------------------------------------------------------------

  /**
   * Build the `plan` block (checklist widget) from the current todos. Re-sent
   * whole on every update under a stable block_id so Slack updates the one widget
   * in place. Task-card BLOCKS use a rich_text `details` (unlike task_update
   * CHUNKS, which take a plain string).
   */
  private buildPlanBlock(todos: WorkPlanTodo[]): AnyBlock {
    const tasks = todos.map((todo) => {
      const detailBits = [todo.source ? `agent ${todo.source}` : '', todo.target ? `[${todo.target}]` : '']
        .filter(Boolean)
        .join(' ');
      const card: Record<string, unknown> = {
        type: 'task_card',
        task_id: `todo:${todo.source || 'main'}::${todo.name}`,
        title: truncate(todo.name, TITLE_MAX),
        status: TODO_STATUS[todo.state] ?? 'pending',
      };
      if (detailBits) card.details = decisionRichText(detailBits, BODY_MAX);
      return card;
    });
    return { type: 'plan', block_id: 'workplan', title: 'Plan', tasks } as unknown as AnyBlock;
  }

  /**
   * Build chunks that flip every still-running step (the rolling activity card
   * and any in-progress reasoning cards) to `complete`, and mark them so in
   * cardStatus. Pure (no I/O) so callers can either flush it live or fold it
   * into the final stop() call.
   */
  private buildCompletionChunks(): AnyChunk[] {
    const chunks: AnyChunk[] = [];
    if (this.currentActivity && this.cardStatus.get(this.currentActivity.id) === 'in_progress') {
      this.cardStatus.set(this.currentActivity.id, 'complete');
      chunks.push({
        type: 'task_update',
        id: this.currentActivity.id,
        title: this.currentActivity.title,
        status: 'complete',
      });
    }
    for (const [key, id] of this.thinkingCardIds) {
      if (this.cardStatus.get(id) === 'in_progress') {
        // Details were already streamed live; just settle the status. Use the
        // structured output's task_state to choose complete vs error.
        const status = reasoningFinalStatus(this.thinkingRaw.get(key) || '');
        this.cardStatus.set(id, status);
        chunks.push({ type: 'task_update', id, title: this.reasoningTitle(key), status });
      }
    }
    return chunks;
  }

  private newStreamer(): ChatStreamer {
    return this.client.chatStream({
      channel: this.opts.channelId,
      thread_ts: this.opts.threadTs,
      recipient_team_id: this.opts.teamId,
      recipient_user_id: this.opts.userId,
      // "plan" groups all the task cards (activity, reasoning) into a SINGLE
      // collapsible plan widget where each card updates in place — rather than
      // "timeline", which lays them out as a linear sequence of steps. Both
      // collapse by default; this is the unified-widget rendering.
      task_display_mode: 'plan',
    });
  }

  /**
   * Append chunks and/or markdown to the stream. Routes to the raw appendStream
   * API when continuing an existing open stream (resume), else to a lazily-created
   * ChatStreamer. If a resumed stream has expired, falls back to starting a fresh
   * stream (a new widget) rather than failing.
   */
  private async streamAppend(args: { chunks?: AnyChunk[]; markdown_text?: string }): Promise<void> {
    if (this.degraded || this.finished) return;
    if (this.resumeStreamTs) {
      try {
        await this.client.chat.appendStream({ channel: this.opts.channelId, ts: this.resumeStreamTs, ...args });
        return;
      } catch (err) {
        // The open stream likely expired during the HITL wait — start fresh.
        logger.warn(
          `could not continue stream ${this.resumeStreamTs}, starting a new one: ${err instanceof Error ? err.message : err}`
        );
        this.resumeStreamTs = undefined;
        this.messageTs = undefined;
      }
    }
    try {
      if (!this.streamer) this.streamer = this.newStreamer();
      const res = await this.streamer.append(args);
      if (res && !this.messageTs && 'ts' in res && res.ts) this.messageTs = res.ts;
    } catch (err) {
      this.degradeTo(err);
    }
  }

  /**
   * Streaming failed (e.g. feature/scope unavailable). Fall back to the legacy
   * single status message so the conversation still gets a reply.
   */
  private degradeTo(err: unknown): void {
    if (this.degraded) return;
    this.degraded = true;
    this.streamer = undefined;
    logger.warn(
      `chat streaming unavailable, falling back to plain status messages: ${err instanceof Error ? err.message : err}`
    );
  }

  private async renderFallbackStatus(): Promise<void> {
    if (!this.degraded || this.finished) return;
    const line = `${this.planTitle}${this.fallbackActivity ? ` [${this.fallbackActivity}]` : ''}${this.fallbackTodos ? `\n${this.fallbackTodos}` : ''}`;
    this.fallbackTs = await postOrUpdateMessage(
      this.client,
      this.opts.channelId,
      this.opts.threadTs,
      line,
      this.fallbackTs
    );
  }
}
