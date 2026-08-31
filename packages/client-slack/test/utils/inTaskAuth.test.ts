/**
 * The `in-task-auth` extension on Slack: A2A's `auth-required` state carries the
 * facts as data, and the card is built from them instead of from a sentence the
 * MCP gateway wrote for the agent. The properties worth pinning are the reading
 * PRECEDENCE (payload → metadata → prose, so a producer that never negotiated
 * the URN still works), that plumbing tool names never reach the user, and that
 * both answers leave with a decision the executor can route.
 */
import { describe, test, expect } from '@jest/globals';
import { Part } from '@a2a-js/sdk';
import {
  AUTH_ACTION_DECLINE,
  AUTH_ACTION_DONE,
  AUTH_ACTION_OPEN,
  authResumeText,
  authorizationDataPart,
  authSubject,
  buildAuthRequiredWidget,
  readAuthRequired,
} from '../../src/utils/inTaskAuth.js';

const AUTH_URL = 'https://gatana.nannos.ringier.ch/api/v1/mcp-servers/oauth/gt_7MOP0ckoiO/begin';
const GATEWAY_TEXT =
  'This tool requires secondary authorization. You must tell the end-user to please go to the authorizeUrl.\n\n' +
  `Please visit the following URL to complete authentication:\n${AUTH_URL}\n\n` +
  'After completing authentication, you can retry your request.';

const textPart = (text: string): Part => ({ kind: 'text', text }) as Part;
const dataPart = (data: Record<string, unknown>): Part => ({ kind: 'data', data }) as Part;

const payloadPart = (overrides: Record<string, unknown> = {}) =>
  dataPart({
    requires_auth: true,
    auth_requirement: {
      service: 'github',
      resource: 'github_get_me',
      auth_methods: [{ method: 'oauth2', description: 'GitHub OAuth', auth_url: AUTH_URL }],
      ...overrides,
    },
  });

function actionElements(blocks: any[]): any[] {
  return blocks.find((b) => b.type === 'actions')?.elements ?? [];
}
function byAction(blocks: any[], actionId: string): any {
  return actionElements(blocks).find((e) => e.action_id === actionId);
}
function decodedValue(blocks: any[], actionId: string): any {
  return JSON.parse(Buffer.from(byAction(blocks, actionId).value, 'base64').toString());
}

describe('readAuthRequired', () => {
  test('the DataPart wins: service and the tool that asked arrive separately', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT), payloadPart()], {
      auth_url: 'https://wrong.example/other',
      tool: 'eval',
    });
    expect(prompt.authUrl).toBe(AUTH_URL);
    expect(prompt.service).toBe('github');
    expect(prompt.tool).toBe('github_get_me');
    // The gateway's own words ride along for the no-URL fallback only.
    expect(prompt.message).toContain('secondary authorization');
  });

  test('falls back to status metadata when no payload came', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT)], { auth_url: AUTH_URL, tool: 'github_get_me' });
    expect(prompt.authUrl).toBe(AUTH_URL);
    expect(prompt.tool).toBe('github_get_me');
    expect(prompt.service).toBeUndefined();
  });

  test('last resort: scrapes the URL out of the prose', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT)], undefined);
    expect(prompt.authUrl).toBe(AUTH_URL);
    expect(prompt.tool).toBeUndefined();
  });

  test('the scrape stops before sentence punctuation', () => {
    // The prose is a paragraph, so the URL is routinely followed by a full stop
    // or a comma — welding one onto the link gives the button a 404.
    const prompt = readAuthRequired([textPart(`Please go to ${AUTH_URL}. Then retry.`)], undefined);
    expect(prompt.authUrl).toBe(AUTH_URL);
    expect(
      readAuthRequired([textPart(`see ${AUTH_URL}, then come back`)], undefined).authUrl
    ).toBe(AUTH_URL);
  });

  test('sandbox plumbing is never named as the thing being authorized', () => {
    // A `need-credentials` raised inside the sandbox is reported against `eval`;
    // "Authorization needed for eval" is worse than naming nothing at all.
    const fromMeta = readAuthRequired([textPart(GATEWAY_TEXT)], { auth_url: AUTH_URL, tool: 'eval' });
    expect(fromMeta.tool).toBeUndefined();
    expect(authSubject(fromMeta)).toBe('');

    const fromPayload = readAuthRequired([payloadPart({ service: 'eval', resource: 'eval' })], undefined);
    expect(fromPayload.service).toBeUndefined();
    expect(fromPayload.tool).toBeUndefined();
  });

  test('a method without a URL is nothing a button can act on', () => {
    const prompt = readAuthRequired(
      [textPart('no link here'), payloadPart({ auth_methods: [{ method: 'ciba', description: 'push' }] })],
      undefined
    );
    expect(prompt.authUrl).toBeUndefined();
    expect(prompt.service).toBe('github');
  });
});

describe('buildAuthRequiredWidget', () => {
  const base = { taskId: 't1', contextId: 'ctx', channelId: 'C1', threadTs: '100.0' };

  test('links the provider and offers both answers', () => {
    const blocks = buildAuthRequiredWidget({ ...base, ...readAuthRequired([textPart(GATEWAY_TEXT), payloadPart()], {}) });

    expect(byAction(blocks, AUTH_ACTION_OPEN).url).toBe(AUTH_URL);
    expect(byAction(blocks, AUTH_ACTION_DONE)).toBeDefined();
    expect(byAction(blocks, AUTH_ACTION_DECLINE).style).toBe('danger');

    // Our own copy names the service in the head and the tool in the body — the
    // gateway's agent-facing prose is not rendered when there is a URL to link.
    const rendered = JSON.stringify(blocks);
    expect(rendered).toContain('Authorization needed for github');
    expect(rendered).toContain('github_get_me');
    expect(rendered).not.toContain('You must tell the end-user');
  });

  test('carries the routing ids the resume re-enters with', () => {
    const blocks = buildAuthRequiredWidget({
      ...base,
      ...readAuthRequired([payloadPart()], {}),
      planMessageTs: '99.1',
      streamMessageTs: '99.2',
    });
    expect(decodedValue(blocks, AUTH_ACTION_DONE)).toMatchObject({
      taskId: 't1',
      contextId: 'ctx',
      channelId: 'C1',
      threadTs: '100.0',
      tool: 'github_get_me',
      subject: 'github',
      planMessageTs: '99.1',
      streamMessageTs: '99.2',
    });
  });

  test('with no URL anywhere, the wire text is shown rather than an empty card', () => {
    const prompt = readAuthRequired([textPart('Authorization is required to continue.')], undefined);
    const blocks = buildAuthRequiredWidget({ ...base, ...prompt });

    expect(byAction(blocks, AUTH_ACTION_OPEN)).toBeUndefined();
    expect(JSON.stringify(blocks)).toContain('Authorization is required to continue.');
    // Declining still has to reach the agent, so the answers stay.
    expect(byAction(blocks, AUTH_ACTION_DONE)).toBeDefined();
    expect(byAction(blocks, AUTH_ACTION_DECLINE)).toBeDefined();
  });
});

describe('the answer sent back', () => {
  test('the DataPart is what the executor routes to the parked interrupt', () => {
    expect(authorizationDataPart('approved')).toEqual({ authorization: { decision: 'approved' } });
    expect(authorizationDataPart('declined')).toEqual({ authorization: { decision: 'declined' } });
  });

  test('the prose beside it names the tool and tells the agent what to do', () => {
    expect(authResumeText('approved', 'github_get_me')).toContain('github_get_me');
    expect(authResumeText('approved', 'github_get_me')).toContain('retry');
    expect(authResumeText('declined')).toContain('Do not ask again');
  });
});
