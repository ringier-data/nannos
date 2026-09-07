/**
 * The client-action round trip's transport pieces, unit-tested where the e2e
 * (engine.test.tsx) can't isolate them: the demux surface, the decision
 * envelope, and the reload-restore path.
 */
import { describe, expect, it } from 'vitest';
import type { AgentResponseData } from '../core/wire';
import { CLIENT_ACTION_EXT } from '../core/extensions';
import { decodeApproval, encodeApproval } from './approval-codec';
import { createDemuxState, demux } from './demux';
import { directiveFromToolArgs } from '../core/client-action';
import { clientActionDirective } from '../core/schemas';
import { findPendingInterrupt, type RestMessageRow } from './index';

const RESULT = { ok: true, applied: ['budget'], rejected: [{ field: 'type', reason: 'bad enum' }] };

function requestEvent(overrides: Partial<{ state: string; data: unknown }> = {}): AgentResponseData {
  return {
    kind: 'status-update',
    contextId: 'conv-1',
    status: {
      state: overrides.state ?? 'input-required',
      message: {
        extensions: [CLIENT_ACTION_EXT],
        parts: [
          {
            kind: 'data',
            data: overrides.data ?? {
              request: { id: 'call-9', directive: { kind: 'apply', target: { type: 'C', id: '7' } } },
            },
          },
        ],
      },
    },
  } as unknown as AgentResponseData;
}

describe('demux: awaited client-action request', () => {
  it('surfaces the request as a marked approval-shaped part and ends the turn', () => {
    const state = createDemuxState('t1-');
    const result = demux(state, requestEvent());
    expect(result.done).toBe('input-required');
    const types = result.chunks.map((c) => c.type);
    expect(types).toEqual(['tool-input-start', 'tool-input-available', 'tool-approval-request']);
    const input = (result.chunks[1] as { input: Record<string, unknown> }).input;
    expect(input._clientActionRequest).toBe(true);
    expect((input.directive as { kind: string }).kind).toBe('apply');
    // Its own part id: the risk gate for this very call may already own a
    // settled part under the raw `call-9` (see `clientActionPartId`).
    expect((result.chunks[2] as { approvalId: string }).approvalId).toBe('call-9#client-action');
    expect((result.chunks[0] as { toolCallId: string }).toolCallId).toBe('call-9#client-action');
    // …and the decision still goes back on the wire under the RAW id.
    expect(decodeApproval('call-9#client-action', { approved: true }).id).toBe('call-9');
  });

  it('fire-and-forget directives still produce NO chunks (the core executes them)', () => {
    const state = createDemuxState('t1-');
    const result = demux(
      state,
      requestEvent({ state: 'working', data: { directive: { kind: 'highlight' } } }),
    );
    expect(result.chunks).toEqual([]);
    expect(result.done).toBeUndefined();
  });
});

describe('approval-codec: the result envelope', () => {
  it('rides approve as versioned JSON and round-trips losslessly', () => {
    const encoded = encodeApproval({ type: 'approve', client_action_result: RESULT });
    expect(encoded.approved).toBe(true);
    // No accidental bypass: the result alone must not read as a bypass grant.
    expect(JSON.parse(encoded.reason!)).toEqual({ v: 1, clientActionResult: RESULT });

    const decoded = decodeApproval('call-9', encoded);
    expect(decoded).toEqual({ id: 'call-9', type: 'approve', client_action_result: RESULT });
  });

  it('bypass semantics are untouched', () => {
    const encoded = encodeApproval({ type: 'approve', bypass_all: true });
    expect(JSON.parse(encoded.reason!)).toEqual({ v: 1, bypass: true, bypass_all: true });
  });
});

describe('read_current_page execution', () => {
  it('answers from the injected reader, sanitized and serialized', async () => {
    const { executeClientAction } = await import('../core/client-action');
    const { ObjectRegistry } = await import('../core/registry');
    const result = await executeClientAction(
      { kind: 'read_current_page' },
      {
        registry: new ObjectRegistry(),
        readCurrentPage: () => ({
          page: { key: '/campaigns/7' },
          rows: ['a', 'b'],
          apiKey: 'sk-never', // the sanitizer's deny list must strip this
        }),
      },
    );
    expect(result.ok).toBe(true);
    const content = JSON.parse((result as { content: string }).content);
    expect(content).toEqual({ page: { key: '/campaigns/7' }, rows: ['a', 'b'] });
  });

  it('is honest when the host provides no reader', async () => {
    const { executeClientAction } = await import('../core/client-action');
    const { ObjectRegistry } = await import('../core/registry');
    const result = await executeClientAction(
      { kind: 'read_current_page' },
      { registry: new ObjectRegistry() },
    );
    expect(result).toEqual({ ok: false, reason: 'unsupported' });
  });

  it('folds the screen outline in under the reserved key when provided', async () => {
    const { executeClientAction } = await import('../core/client-action');
    const { ObjectRegistry } = await import('../core/registry');
    const result = await executeClientAction(
      { kind: 'read_current_page' },
      {
        registry: new ObjectRegistry(),
        readCurrentPage: () => ({ page: { key: '/campaigns/7' } }),
        screenOutline: () => '# Campaign 7\n12 line items',
      },
    );
    const content = JSON.parse((result as { content: string }).content);
    expect(content.page).toEqual({ key: '/campaigns/7' });
    expect(content.screen).toBe('# Campaign 7\n12 line items');
  });

  it('the outline alone answers a host that wired no reader', async () => {
    const { executeClientAction } = await import('../core/client-action');
    const { ObjectRegistry } = await import('../core/registry');
    const result = await executeClientAction(
      { kind: 'read_current_page' },
      { registry: new ObjectRegistry(), screenOutline: () => '# All the page says' },
    );
    const content = JSON.parse((result as { content: string }).content);
    expect(content).toEqual({ screen: '# All the page says' });
  });
});

describe('history restore: a parked request self-heals on reload', () => {
  const requestRow = (ts: string): RestMessageRow => ({
    kind: 'status-update',
    state: 'input-required',
    created_at: ts,
    raw_payload: JSON.stringify({
      status: {
        state: 'input-required',
        message: {
          extensions: [CLIENT_ACTION_EXT],
          parts: [
            { kind: 'data', data: { request: { id: 'call-9', directive: { kind: 'apply' } } } },
          ],
        },
      },
    }),
  });

  it('restores the pending request as a marked, machine-answerable action', () => {
    const restored = findPendingInterrupt([requestRow('2026-08-26T10:00:00Z')]);
    expect(restored).not.toBeNull();
    expect(restored!.actionRequests).toEqual([
      {
        name: 'client_action',
        args: {
          directive: { kind: 'apply' },
          _clientActionRequest: true,
          _call_id: 'call-9#client-action',
        },
      },
    ]);
  });

  it('a later resolving status means nothing is restored', () => {
    const resolvedRow: RestMessageRow = {
      kind: 'status-update',
      state: 'completed',
      created_at: '2026-08-26T10:01:00Z',
      raw_payload: JSON.stringify({ status: { state: 'completed' } }),
    };
    expect(findPendingInterrupt([requestRow('2026-08-26T10:00:00Z'), resolvedRow])).toBeNull();
  });
});

describe('directiveFromToolArgs: one pause instead of two', () => {
  // The card carries the agent's FLAT tool args; the wire directive is nested.
  // Building one from the other is what lets the host execute on approve and
  // answer with the result, so the agent resumes ONCE.
  const gateArgs = {
    _call_id: 'tooluse_x',
    _risk_metadata: { score: 0.9 },
    _summary: 'ignored',
    kind: 'apply',
    target_type: 'Campaign',
    target_id: 'new',
    values: { name: 'Test Campaign 001', budget: '25000' },
    confirm: true,
  };

  it('nests the target and drops the card-only keys', () => {
    expect(directiveFromToolArgs(gateArgs)).toEqual({
      kind: 'apply',
      target: { type: 'Campaign', id: 'new' },
      values: { name: 'Test Campaign 001', budget: '25000' },
    });
  });

  it('what it builds passes the directive schema the executor validates', () => {
    expect(clientActionDirective.safeParse(directiveFromToolArgs(gateArgs)).success).toBe(true);
  });

  it('carries highlight/navigate/read_current_page too', () => {
    expect(
      directiveFromToolArgs({ kind: 'highlight', target_type: 'Campaign', target_id: '7', field: 'budget' }),
    ).toEqual({ kind: 'highlight', target: { type: 'Campaign', id: '7' }, field: 'budget' });
    expect(directiveFromToolArgs({ kind: 'navigate', to: '/campaigns/7' })).toEqual({
      kind: 'navigate',
      to: '/campaigns/7',
    });
    expect(directiveFromToolArgs({ kind: 'read_current_page' })).toEqual({ kind: 'read_current_page' });
  });

  it('returns null rather than guessing — the round trip then answers', () => {
    // Each of these must fall back to a plain approve, never a wrong directive.
    expect(directiveFromToolArgs({ kind: 'apply', values: {} })).toBeNull(); // no target
    expect(directiveFromToolArgs({ kind: 'navigate' })).toBeNull(); // no path
    expect(directiveFromToolArgs({ kind: 'refresh', target_type: 'X', target_id: '1' })).toBeNull();
    expect(directiveFromToolArgs({ target_type: 'X', target_id: '1' })).toBeNull(); // no kind
    expect(directiveFromToolArgs(null)).toBeNull();
    expect(directiveFromToolArgs('nope')).toBeNull();
  });
});
