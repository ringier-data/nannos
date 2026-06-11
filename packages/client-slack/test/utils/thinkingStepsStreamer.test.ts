import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { WebClient } from '@slack/web-api';
import { ThinkingStepsStreamer, WorkPlanTodo } from '../../src/utils/thinkingStepsStreamer.js';

// ---------------------------------------------------------------------------
// Mock WebClient with a recording chatStream
// ---------------------------------------------------------------------------

interface AppendCall {
  markdown_text?: string;
  chunks?: any[];
}

function mockClient() {
  const appendCalls: AppendCall[] = [];
  const stopCalls: any[] = [];
  let started = false;

  const streamer = {
    append: jest.fn(async (args: AppendCall) => {
      appendCalls.push(args);
      if (!started) {
        started = true;
        return { ok: true, ts: '111.222' };
      }
      return { ok: true };
    }),
    stop: jest.fn(async (args: any) => {
      stopCalls.push(args ?? {});
      return { ok: true };
    }),
  };

  const client = {
    chatStream: jest.fn(() => streamer),
    chat: {
      postMessage: jest.fn(async () => ({ ok: true, ts: '999.000' })),
      update: jest.fn(async () => ({ ok: true })),
      delete: jest.fn(async () => ({ ok: true })),
    },
  } as unknown as WebClient;

  return { client, streamer, appendCalls, stopCalls };
}

const baseOpts = { channelId: 'C1', threadTs: '100.0', teamId: 'T1', userId: 'U1' };

/** Flatten every chunk pushed across all append calls. */
function allChunks(appendCalls: AppendCall[]): any[] {
  return appendCalls.flatMap((c) => c.chunks ?? []);
}

/** All task_update chunks (card ids carry a random per-instance prefix). */
function taskCards(appendCalls: AppendCall[]): any[] {
  return allChunks(appendCalls).filter((c) => c.type === 'task_update');
}

describe('ThinkingStepsStreamer', () => {
  let m: ReturnType<typeof mockClient>;

  beforeEach(() => {
    m = mockClient();
  });

  test('start() emits a plan_update + in-progress placeholder card and captures the ts', async () => {
    const s = new ThinkingStepsStreamer(m.client, { ...baseOpts, initialTitle: 'Working' });
    await s.start();
    const planChunks = allChunks(m.appendCalls).filter((c) => c.type === 'plan_update');
    expect(planChunks).toHaveLength(1);
    expect(planChunks[0].title).toBe('Working');
    // A spinning placeholder card gives the message immediate content (not empty).
    const placeholder = allChunks(m.appendCalls).find((c) => c.type === 'task_update');
    expect(placeholder.id.endsWith(':act:boot')).toBe(true);
    expect(placeholder.status).toBe('in_progress');
    expect(placeholder.title.length).toBeGreaterThan(0);
    expect(s.ts).toBe('111.222');
  });

  test('applyWorkPlan posts a separate plan-block message and updates it in place', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    const todos: WorkPlanTodo[] = [
      { name: 'plan', state: 'submitted' },
      { name: 'work', state: 'working', source: 'researcher' },
      { name: 'done', state: 'completed' },
      { name: 'oops', state: 'failed' },
    ];
    await s.applyWorkPlan(todos);

    // Posted as its OWN message (not a stream chunk) so it never shares a message
    // with the timeline's task cards (Slack: plan + task blocks are exclusive).
    const post = (m.client.chat.postMessage as jest.Mock).mock.calls[0][0] as any;
    const plan = post.blocks[0];
    expect(plan.type).toBe('plan');
    expect(plan.block_id).toBe('workplan');
    expect(plan.tasks.map((t: any) => t.status)).toEqual(['pending', 'in_progress', 'complete', 'error']);
    expect(plan.tasks[0].task_id).toBe('todo:main::plan');
    expect(plan.tasks[1].details.type).toBe('rich_text');
    expect(JSON.stringify(plan.tasks[1].details)).toContain('agent researcher');
    // todos do NOT leak into the streamed timeline as task_update chunks
    expect(allChunks(m.appendCalls).some((c) => c.type === 'task_update')).toBe(false);

    // Second update edits the SAME message in place (chat.update on its ts).
    await s.applyWorkPlan([{ name: 'plan', state: 'completed' }]);
    const update = (m.client.chat.update as jest.Mock).mock.calls.at(-1)![0] as any;
    expect(update.ts).toBe('999.000');
    expect(update.blocks[0].tasks[0].status).toBe('complete');
  });

  test('applyWorkPlan updates an existing plan message (carried across a HITL resume)', async () => {
    // Resume turn: streamer seeded with the prior plan message ts.
    const s = new ThinkingStepsStreamer(m.client, { ...baseOpts, planMessageTs: 'prior.plan.ts' });
    await s.applyWorkPlan([{ name: 'do it', state: 'completed' }]);
    // It updates the existing widget in place — no new plan message is posted.
    expect(m.client.chat.postMessage).not.toHaveBeenCalled();
    const update = (m.client.chat.update as jest.Mock).mock.calls.at(-1)![0] as any;
    expect(update.ts).toBe('prior.plan.ts');
    expect(update.blocks[0].type).toBe('plan');
    expect(s.planTs).toBe('prior.plan.ts');
  });

  test('applyWorkPlan falls back to a text checklist when plan blocks are rejected', async () => {
    const client = {
      chatStream: jest.fn(() => ({ append: jest.fn(async () => ({ ts: '1.1' })), stop: jest.fn() })),
      chat: {
        postMessage: jest
          .fn<any>()
          .mockRejectedValueOnce(new Error('invalid_blocks')) // plan block rejected
          .mockResolvedValue({ ok: true, ts: 'plan.ts' }), // text fallback succeeds
        update: jest.fn(async () => ({ ok: true })),
        delete: jest.fn(async () => ({ ok: true })),
      },
    } as unknown as WebClient;
    const s = new ThinkingStepsStreamer(client, baseOpts);
    await s.applyWorkPlan([{ name: 'do the thing', state: 'working' }]);

    // Second call (after the block path failed) goes straight to the text path.
    await s.applyWorkPlan([{ name: 'do the thing', state: 'completed' }]);
    const calls = (client.chat.postMessage as jest.Mock).mock.calls;
    // The fallback post carries markdown_text, not blocks.
    const textPost = calls.find((c: any) => c[0].markdown_text)?.[0] as any;
    expect(textPost.markdown_text).toContain('do the thing');
  });

  test('applyActivity rolls a spinner: newest in_progress, previous completes', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.applyActivity('Delegating to polymath', 'orchestrator');
    let cards = taskCards(m.appendCalls);
    // First activity → in_progress (Slack animates a spinner on it). Agent name
    // is prefixed into the title; no `details` (which would add a collapse chevron).
    expect(cards).toHaveLength(1);
    expect(cards[0].status).toBe('in_progress');
    expect(cards[0].title).toBe('orchestrator: Delegating to polymath');
    expect(cards[0].details).toBeUndefined();
    const firstId = cards[0].id;

    // Second activity → previous flips to complete, new one in_progress.
    await s.applyActivity('Running ls', 'polymath');
    cards = taskCards(m.appendCalls);
    const firstLatest = cards.filter((c) => c.id === firstId).at(-1);
    const second = cards.find((c) => c.title === 'polymath: Running ls');
    expect(firstLatest.status).toBe('complete');
    expect(second.status).toBe('in_progress');

    // finish() completes the last running activity
    await s.finish();
    expect(m.stopCalls[0].chunks?.some((c: any) => c.id === second.id && c.status === 'complete')).toBe(true);
  });

  test('first activity reuses the bootstrap placeholder card in place', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    const bootId = taskCards(m.appendCalls)[0].id; // the seeded placeholder
    await s.applyActivity('Agent execution started.');
    // The first real activity reuses the placeholder rather than stacking a new card.
    const boot = taskCards(m.appendCalls).filter((c) => c.id === bootId).at(-1);
    expect(boot.title).toBe('Agent execution started.');
    expect(boot.status).toBe('in_progress');
    // Only the one (bootstrap) card id exists — no separate activity card was created.
    expect(new Set(taskCards(m.appendCalls).map((c) => c.id))).toEqual(new Set([bootId]));
  });

  test('applyActivity truncates long titles to 256', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    const long = 'x'.repeat(300);
    await s.applyActivity(long);
    const card = allChunks(m.appendCalls).find((c) => c.type === 'task_update');
    expect(card.status).toBe('in_progress');
    expect(card.title.length).toBe(256);
    expect(card.title.endsWith('…')).toBe(true);
  });

  test('appendThinking streams plain-prose reasoning live as display deltas', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.appendThinking('The user wants', 'planner');
    await s.appendThinking(' to delegate.', 'planner');
    const cards = taskCards(m.appendCalls);
    expect(cards[0].title).toBe('Reasoning · planner');
    expect(cards[0].status).toBe('in_progress');
    // Each chunk streams its new display text live (details append in Slack).
    expect(cards[0].details).toBe('The user wants');
    expect(cards[1].details).toBe(' to delegate.');
  });

  test('appendThinking streams ONLY $.message from a structured-JSON envelope (mid-stream)', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    // JSON arrives in pieces; the scaffolding must never leak into details.
    await s.appendThinking('{"task_state":"completed","message":"Here are ', 'gp');
    await s.appendThinking('the details."}', 'gp');
    const detailsStreamed = taskCards(m.appendCalls)
      .map((c) => c.details || '')
      .join('');
    expect(detailsStreamed).toBe('Here are the details.');
    // task_state=completed → card settles to complete.
    await s.finish();
    const done = (m.stopCalls[0].chunks || []).find((c: any) => c.title === 'Reasoning · gp');
    expect(done.status).toBe('complete');
  });

  test('reasoning card settles to error when task_state indicates failure', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.appendThinking('{"task_state":"failed","message":"could not complete"}', 'gp');
    await s.finish();
    const done = (m.stopCalls[0].chunks || []).find((c: any) => c.title === 'Reasoning · gp');
    expect(done.status).toBe('error');
  });

  test('a resumed streamer continues the existing open stream; the answer is its own new message', async () => {
    const appendStream = jest.fn<any>(async () => ({ ok: true }));
    const stopStream = jest.fn<any>(async () => ({ ok: true }));
    const answerAppend = jest.fn<any>(async () => ({ ok: true, ts: 'answer.ts' }));
    const answerStop = jest.fn<any>(async () => ({ ok: true }));
    const chatStream = jest.fn(() => ({ append: answerAppend, stop: answerStop }));
    const client = {
      chatStream,
      chat: { appendStream, stopStream, postMessage: jest.fn(), update: jest.fn(), delete: jest.fn() },
    } as unknown as WebClient;

    const s = new ThinkingStepsStreamer(client, { ...baseOpts, resumeStreamTs: 'open.ts' });
    expect(s.ts).toBe('open.ts'); // adopts the existing thinking message immediately
    await s.start(); // re-activates the plan title on the existing stream
    await s.applyActivity('Running ls', 'polymath');
    await s.appendAnswer('done');
    await s.finish();

    // Thinking-widget writes continue the existing stream (open.ts), never startStream.
    expect((appendStream as jest.Mock).mock.calls.every((c: any) => c[0].ts === 'open.ts')).toBe(true);
    expect(((stopStream as jest.Mock).mock.calls[0][0] as any).ts).toBe('open.ts');
    // The final answer is streamed into its OWN new message (separate chatStream).
    expect(chatStream).toHaveBeenCalledTimes(1);
    expect(answerAppend).toHaveBeenCalledWith({ markdown_text: 'done' });
    expect(s.answerTs).toBe('answer.ts');
  });

  test('pause() leaves the stream open (no stop) for the HITL resume', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    await s.applyActivity('Running ls', 'polymath');
    await s.pause('Awaiting your approval');
    // Did NOT stop the stream — the resume continues it.
    expect(m.streamer.stop).not.toHaveBeenCalled();
    // Relabeled the plan and completed the in-progress step.
    const plan = allChunks(m.appendCalls).filter((c) => c.type === 'plan_update').at(-1);
    expect(plan.title).toBe('Awaiting your approval');
  });

  test('appendAnswer adds a "Generating response" step to the thinking widget, completed on finish', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    await s.appendAnswer('the answer');
    // A spinner step appears in the thinking widget while the answer is produced.
    const gen = taskCards(m.appendCalls).find((c) => c.title === 'Generating response…');
    expect(gen).toBeDefined();
    expect(gen.status).toBe('in_progress');
    // On finish it settles to complete (in the thinking widget's stop chunks).
    await s.finish();
    const done = m.stopCalls
      .flatMap((c: any) => c.chunks || [])
      .find((c: any) => c.title === 'Generating response…' && c.status === 'complete');
    expect(done).toBeDefined();
  });

  test('appendAnswer streams markdown_text and flips in-progress thinking cards to complete', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.appendThinking('thinking…', 'planner');
    await s.appendAnswer('Here is the answer.');
    // a complete update for the thinking card was emitted
    const completed = allChunks(m.appendCalls).find(
      (c) => c.title === 'Reasoning · planner' && c.status === 'complete'
    );
    expect(completed).toBeDefined();
    // the answer went out as markdown_text (the only markdown), not a chunk
    const md = m.appendCalls.find((c) => c.markdown_text === 'Here is the answer.');
    expect(md).toBeDefined();
    expect(s.hasAnswer).toBe(true);
  });

  test('appendAnswer dedupes a terminal snapshot against live-streamed deltas', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    // Live artifact-append: first chunk is a create (snapshot), rest are deltas.
    await s.appendAnswer('Here are ', true);
    await s.appendAnswer('the results.', false);
    // Terminal status carries the SAME full answer as an authoritative snapshot.
    await s.appendAnswer('Here are the results.', true);

    const markdowns = m.appendCalls.filter((c) => c.markdown_text !== undefined).map((c) => c.markdown_text);
    // Only the two original pieces were streamed — the terminal snapshot added nothing.
    expect(markdowns).toEqual(['Here are ', 'the results.']);
  });

  test('appendAnswer tops up a snapshot suffix when a live delta was dropped', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    await s.appendAnswer('Here are ', true); // a frame was lost; "the results." never arrived
    await s.appendAnswer('Here are the results.', true); // terminal full snapshot

    const markdowns = m.appendCalls.filter((c) => c.markdown_text !== undefined).map((c) => c.markdown_text);
    expect(markdowns).toEqual(['Here are ', 'the results.']);
  });

  test('appendAnswer dedupes a full snapshot re-sent through a second channel', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    const full = 'The complete answer.';
    await s.appendAnswer(full, true);
    await s.appendAnswer(full, true); // duplicate full delivery
    const markdowns = m.appendCalls.filter((c) => c.markdown_text !== undefined).map((c) => c.markdown_text);
    expect(markdowns).toEqual([full]);
    expect(s.hasAnswer).toBe(true);
  });

  test('finish() finalizes both streams (trailing → answer, blocks → thinking) and is idempotent', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    await s.appendAnswer('answer');
    await s.finish({ trailingMarkdown: '\n\nAttached files:\nhttp://x', blocks: [{ type: 'divider' } as any] });
    const stopsAfterFirst = (m.streamer.stop as jest.Mock).mock.calls.length;
    await s.finish(); // second call no-ops
    expect((m.streamer.stop as jest.Mock).mock.calls.length).toBe(stopsAfterFirst);
    // Trailing file links go to the answer message; blocks to the thinking widget.
    expect(m.stopCalls.some((c: any) => (c.markdown_text || '').includes('Attached files'))).toBe(true);
    expect(m.stopCalls.some((c: any) => c.blocks?.length === 1)).toBe(true);
  });

  test('discard() stops an open stream and deletes the message', async () => {
    const s = new ThinkingStepsStreamer(m.client, baseOpts);
    await s.start();
    await s.discard();
    expect(m.streamer.stop).toHaveBeenCalledTimes(1);
    expect(m.client.chat.delete).toHaveBeenCalledWith({ channel: 'C1', ts: '111.222' });
  });

  test('degrades to plain status messages when streaming throws', async () => {
    // chatStream returns a streamer whose append rejects → triggers degraded mode
    const failingStreamer = {
      append: jest.fn(async () => {
        throw new Error('missing_scope');
      }),
      stop: jest.fn(async () => ({ ok: true })),
    };
    const client = {
      chatStream: jest.fn(() => failingStreamer),
      chat: {
        postMessage: jest.fn(async () => ({ ok: true, ts: 'fb.1' })),
        update: jest.fn(async () => ({ ok: true })),
        delete: jest.fn(async () => ({ ok: true })),
      },
    } as unknown as WebClient;

    const s = new ThinkingStepsStreamer(client, baseOpts);
    await s.start(); // append throws → degraded
    await s.applyWorkPlan([{ name: 'do it', state: 'working' }]);
    // fell back to a plain status message
    expect(client.chat.postMessage).toHaveBeenCalled();

    await s.appendAnswer('final answer');
    await s.finish();
    // the final answer was posted/updated as a normal message
    const update = (client.chat.update as jest.Mock).mock.calls.length;
    const post = (client.chat.postMessage as jest.Mock).mock.calls.length;
    expect(update + post).toBeGreaterThan(0);
  });
});
