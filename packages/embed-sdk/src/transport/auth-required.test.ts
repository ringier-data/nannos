/**
 * `auth-required` is a TERMINAL A2A state that carries an authorize URL. The
 * demux must end the turn on it (or the panel streams forever and the composer
 * stays locked) and must NOT render the status text: that text is the MCP
 * gateway talking to the agent, and no LLM ever rewrites it.
 */
import { describe, expect, it } from 'vitest';
import { createDemuxState, demux } from './demux';
import type { AgentResponseData } from '../core/wire';

const AUTH_URL = 'https://gatana.nannos.ringier.ch/api/v1/mcp-servers/oauth/gt_7MOP0ckoiO/begin';
const GATEWAY_TEXT =
  'This tool requires secondary authorization. You must tell the end-user to please go to the authorizeUrl.\n\n' +
  `Please visit the following URL to complete authentication:\n${AUTH_URL}\n\n` +
  'After completing authentication, you can retry your request.';

/** The real status-update shape, as captured off the socket. */
function authEvent(overrides: Partial<AgentResponseData> = {}): AgentResponseData {
  return {
    kind: 'status-update',
    taskId: '924c95ed-ac75-4c56-80dd-862cb9fc26f2',
    status: {
      state: 'TASK_STATE_AUTH_REQUIRED',
      message: { role: 'ROLE_AGENT', parts: [{ text: GATEWAY_TEXT }] },
    },
    metadata: {
      subagent: null,
      requires_auth: true,
      auth_url: AUTH_URL,
      error_code: 'need-credentials',
      tool: 'eval',
    },
    ...overrides,
  };
}

describe('demux: auth-required', () => {
  it('ends the turn (terminal state)', () => {
    const result = demux(createDemuxState('t-'), authEvent());
    expect(result.done).toBe('terminal');
  });

  it('emits one structured auth part with the URL and the tool', () => {
    const result = demux(createDemuxState('t-'), authEvent());
    const parts = result.chunks.filter((c) => c.type === 'data-auth-required');
    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({
      id: 't-auth',
      data: { authUrl: AUTH_URL, tool: 'eval' },
    });
  });

  it('never renders the gateway text as assistant text', () => {
    const result = demux(createDemuxState('t-'), authEvent());
    const rendered = result.chunks
      .filter((c) => c.type === 'text-delta')
      .map((c) => (c as { delta: string }).delta)
      .join('');
    expect(rendered).toBe('');
    // It rides the structured part instead, for the no-URL fallback and dev mode.
    const part = result.chunks.find((c) => c.type === 'data-auth-required') as {
      data: { message?: string };
    };
    expect(part.data.message).toContain('secondary authorization');
  });

  it('scrapes the URL from the text when metadata carries none', () => {
    const result = demux(createDemuxState('t-'), authEvent({ metadata: undefined }));
    const part = result.chunks.find((c) => c.type === 'data-auth-required') as {
      data: { authUrl?: string; tool?: string };
    };
    expect(part.data.authUrl).toBe(AUTH_URL);
    expect(part.data.tool).toBeUndefined();
  });

  it('keeps text streamed before the interrupt and closes it', () => {
    const state = createDemuxState('t-');
    demux(state, {
      kind: 'artifact-update',
      artifact: { parts: [{ text: 'Let me look that up… ' }] },
      turnOffset: 21,
    });
    const result = demux(state, authEvent());
    expect(result.chunks.some((c) => c.type === 'text-end')).toBe(true);
    expect(state.textBuffer).toBe('Let me look that up… ');
    expect(result.done).toBe('terminal');
  });
});
