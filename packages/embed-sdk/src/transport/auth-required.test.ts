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

/**
 * The `in-task-auth` extension: the same terminal state, now carrying the facts
 * as data instead of leaving the client to parse a sentence written for the
 * agent. The extension is additive, so the two properties worth pinning are
 * that the data part wins when present, and that nothing regresses when a
 * producer never sent one.
 */
describe('demux: auth-required with the in-task-auth DataPart', () => {
  const PAYLOAD = {
    requires_auth: true,
    auth_requirement: {
      service: 'github',
      resource: 'github_get_me',
      auth_methods: [{ method: 'oauth2', description: 'GitHub OAuth', auth_url: AUTH_URL }],
      required_scopes: ['repo'],
    },
    correlation_id: 'call-1',
  };

  function structuredEvent(payload: unknown = PAYLOAD): AgentResponseData {
    return {
      kind: 'status-update',
      taskId: '924c95ed-ac75-4c56-80dd-862cb9fc26f2',
      status: {
        state: 'TASK_STATE_AUTH_REQUIRED',
        message: {
          role: 'ROLE_AGENT',
          parts: [{ text: GATEWAY_TEXT }, { data: payload }],
          extensions: ['urn:nannos:a2a:in-task-auth:1.0'],
        },
      },
      // Deliberately no `auth_url` / `tool` metadata: the data part is the source.
      metadata: { requires_auth: true },
    } as AgentResponseData;
  }

  function authPart(event: AgentResponseData) {
    const result = demux(createDemuxState('t-'), event);
    return (
      result.chunks.find((c) => c.type === 'data-auth-required') as {
        data: { authUrl?: string; tool?: string; service?: string };
      }
    ).data;
  }

  it('reads the URL, the service and the tool from the payload', () => {
    expect(authPart(structuredEvent())).toMatchObject({
      authUrl: AUTH_URL,
      service: 'github',
      tool: 'github_get_me',
    });
  });

  it('prefers the payload over the status metadata', () => {
    const event = structuredEvent();
    event.metadata = { auth_url: 'https://stale.example/begin', tool: 'eval' };
    expect(authPart(event)).toMatchObject({ authUrl: AUTH_URL, tool: 'github_get_me' });
  });

  it('does not reach the prose-scraping fallback when the payload has a URL', () => {
    const event = structuredEvent({
      ...PAYLOAD,
      auth_requirement: {
        ...PAYLOAD.auth_requirement,
        auth_methods: [{ method: 'oauth2', auth_url: 'https://from-payload.example/begin' }],
      },
    });
    expect(authPart(event).authUrl).toBe('https://from-payload.example/begin');
  });

  it('skips a method with no URL and takes the first that has one', () => {
    const event = structuredEvent({
      ...PAYLOAD,
      auth_requirement: {
        ...PAYLOAD.auth_requirement,
        auth_methods: [
          { method: 'bearer_token', description: 'paste a token' },
          { method: 'oauth2', auth_url: AUTH_URL },
        ],
      },
    });
    expect(authPart(event).authUrl).toBe(AUTH_URL);
  });

  it('degrades to the metadata path when the producer sent no data part', () => {
    expect(authPart(authEvent())).toMatchObject({ authUrl: AUTH_URL, tool: 'eval' });
    expect(authPart(authEvent()).service).toBeUndefined();
  });

  it('ignores a data part that is not an auth payload', () => {
    // A work-plan part riding the same status must not be mistaken for the
    // requirement: the reader falls through to metadata, then to the prose.
    const data = authPart(structuredEvent({ todos: [] }));
    expect(data.authUrl).toBe(AUTH_URL);
    expect(data.tool).toBeUndefined();
    expect(data.service).toBeUndefined();
  });
});
