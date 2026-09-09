/**
 * `TurnSession`'s stale-prompt suppression, which is only sound while an
 * approval id identifies one ASK rather than one (tool, args).
 *
 * The rule exists to absorb a `pendingHitl` snapshot redelivered by a subscribe
 * racing a resume. Its hazard is the mirror image: when the server stamped
 * `_call_id` with a hash of tool + args, a program that repeated an approved
 * call asked a GENUINE second question under the id just answered — the card was
 * suppressed, no decision could be submitted, and the turn hung on "Working…".
 */
import { describe, expect, it } from 'vitest';
import type { AgentResponseData } from '../core/wire';
import { HITL_EXT } from '../core/extensions';
import { TurnSession } from './turn-session';

const CALL_KEY = 'ls:eeae2bc927083f4f';

/** A HITL approval prompt for `ls({path:'/'})`, asked under `callId`. */
function askEvent(callId: string): AgentResponseData {
  return {
    kind: 'status-update',
    contextId: 'conv-1',
    status: {
      state: 'input-required',
      message: {
        extensions: [HITL_EXT],
        parts: [
          { kind: 'text', text: "Tool 'ls' has risk score 1.00 (threshold: 0.80)" },
          {
            kind: 'data',
            data: {
              action_requests: [
                {
                  name: 'ls',
                  args: { path: '/', _call_id: callId },
                  description: "Tool 'ls' has risk score 1.00 (threshold: 0.80)",
                },
              ],
              review_configs: [{ action_name: 'ls', allowed_decisions: ['approve', 'reject'] }],
            },
          },
        ],
      },
    },
  } as unknown as AgentResponseData;
}

function sessionAnswering(callId: string): TurnSession {
  return new TurnSession('conv-1', {
    startMessageId: null,
    answeredApprovals: [{ id: callId, kind: 'hitl' }],
  });
}

describe('TurnSession: already-answered approval prompts', () => {
  it('suppresses the SAME ask redelivered', () => {
    const askId = `${CALL_KEY}@c1:0`;
    const session = sessionAnswering(askId);
    const before = session.chunksEmitted;

    session.handle(askEvent(askId));

    expect(session.chunksEmitted).toBe(before);
    expect(session.closed).toBe(false);
  });

  it('renders a NEW ask about the same call, asked under a different id', () => {
    // Same tool, same args — so the same `call_key` — but a second question, in a
    // later `eval` call. Both asks carried one id before ask ids existed.
    const answered = `${CALL_KEY}@c1:0`;
    const session = sessionAnswering(answered);
    const before = session.chunksEmitted;

    session.handle(askEvent(`${CALL_KEY}@c2:0`));

    expect(session.chunksEmitted).toBeGreaterThan(before);
  });

  it('renders a new ask even when the answered id is a bare call key', () => {
    // Mid-deploy: this turn was opened by answering a pre-ask-id prompt, and the
    // reconnected backend now stamps ask ids. The new ask must still render.
    const session = sessionAnswering(CALL_KEY);
    const before = session.chunksEmitted;

    session.handle(askEvent(`${CALL_KEY}@c2:0`));

    expect(session.chunksEmitted).toBeGreaterThan(before);
  });
});
