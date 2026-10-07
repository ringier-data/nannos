/**
 * The server's reading of a reply the user TYPED at an approval card
 * (`hitl-decision` extension): demuxed live into a durable part the thread
 * settles that card from.
 */
import { describe, expect, it } from 'vitest';
import type { AgentResponseData } from '../core/wire';
import { HITL_DECISION_EXT } from '../core/extensions';
import { createDemuxState, demux } from './demux';
import { typedDecisionsById } from './history-mapper';
import type { NannosUIMessage } from './ai-types';

const DECISIONS = [{ id: 'call-9', type: 'approve', intent: 'approve' }];

const decisionEvent = (decisions: unknown = DECISIONS): AgentResponseData =>
  ({
    kind: 'status-update',
    contextId: 'conv-1',
    status: {
      state: 'working',
      message: { extensions: [HITL_DECISION_EXT], parts: [{ kind: 'data', data: { decisions } }] },
    },
  }) as unknown as AgentResponseData;

describe('demux: hitl-decision', () => {
  it('becomes one durable data part, and the turn goes on', () => {
    const state = createDemuxState('t1-');
    const result = demux(state, decisionEvent());
    expect(result.done).toBeUndefined();
    expect(result.chunks).toHaveLength(1);
    expect(result.chunks[0]).toMatchObject({
      type: 'data-hitl-decision',
      id: 't1-hitl-decision-call-9',
      data: { decisions: DECISIONS },
    });
    // Durable: a reset-step replay keeps it.
    expect(state.durable.some((p) => p.type === 'data-hitl-decision')).toBe(true);
  });

  it('an empty reading produces nothing', () => {
    expect(demux(createDemuxState('t1-'), decisionEvent([])).chunks).toEqual([]);
  });

  it('is found thread-wide by the card it answers', () => {
    const messages = [
      {
        id: 'a',
        role: 'assistant',
        parts: [{ type: 'data-hitl-decision', id: 'd', data: { decisions: DECISIONS } }],
      },
    ] as NannosUIMessage[];
    expect(typedDecisionsById(messages).get('call-9')).toEqual(DECISIONS[0]);
  });
});
