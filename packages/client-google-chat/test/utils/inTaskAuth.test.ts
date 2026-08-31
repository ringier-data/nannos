/**
 * The `in-task-auth` extension on Google Chat: A2A's `auth-required` state
 * carries the facts as data, and the card is built from them instead of from a
 * sentence the MCP gateway wrote for the agent. The properties worth pinning are
 * the reading PRECEDENCE (payload → metadata → prose, so a producer that never
 * negotiated the URN still works), that plumbing tool names never reach the
 * user, and that both answers leave with a decision the executor can route.
 */
import { describe, test, expect } from '@jest/globals';
import { Part } from '@a2a-js/sdk';
import {
  authResumeText,
  authorizationDataPart,
  authSubject,
  readAuthRequired,
} from '../../src/utils/inTaskAuth.js';
import { GoogleChatService } from '../../src/services/googleChatService.js';
import { Config } from '../../src/config/config.js';

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

describe('readAuthRequired', () => {
  test('the DataPart wins: service and the tool that asked arrive separately', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT), payloadPart()], {
      auth_url: 'https://wrong.example/other',
      tool: 'eval',
    });
    expect(prompt.authUrl).toBe(AUTH_URL);
    expect(prompt.service).toBe('github');
    expect(prompt.tool).toBe('github_get_me');
    expect(prompt.message).toContain('secondary authorization');
  });

  test('falls back to status metadata, then to scraping the prose', () => {
    const fromMeta = readAuthRequired([textPart(GATEWAY_TEXT)], { auth_url: AUTH_URL, tool: 'github_get_me' });
    expect(fromMeta.authUrl).toBe(AUTH_URL);
    expect(fromMeta.tool).toBe('github_get_me');

    const scraped = readAuthRequired([textPart(GATEWAY_TEXT)], undefined);
    expect(scraped.authUrl).toBe(AUTH_URL);
    expect(scraped.tool).toBeUndefined();
  });

  test('sandbox plumbing is never named as the thing being authorized', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT)], { auth_url: AUTH_URL, tool: 'eval' });
    expect(prompt.tool).toBeUndefined();
    expect(authSubject(prompt)).toBe('');
  });
});

describe('buildInTaskAuthCard', () => {
  const config = { baseUrl: 'https://chat.nannos.example' } as Config;
  const service = Object.create(GoogleChatService.prototype) as GoogleChatService;

  function buttons(card: any): any[] {
    const widgets = card.card.sections[0].widgets;
    return widgets.find((w: any) => w.buttonList)?.buttonList.buttons ?? [];
  }
  function answerParams(card: any, action: string): any {
    const btn = buttons(card).find(
      (b: any) => b.onClick?.action?.parameters?.some((p: any) => p.key === 'action' && p.value === action)
    );
    const raw = btn.onClick.action.parameters.find((p: any) => p.key === 'parameters').value;
    return JSON.parse(raw);
  }

  test('links the provider and offers both answers', () => {
    const prompt = readAuthRequired([textPart(GATEWAY_TEXT), payloadPart()], {});
    const card = service.buildInTaskAuthCard(config, prompt, { taskId: 't1' });

    expect(buttons(card).find((b: any) => b.onClick?.openLink)?.onClick.openLink.url).toBe(AUTH_URL);
    expect(answerParams(card, 'approved')).toMatchObject({ taskId: 't1', tool: 'github_get_me', subject: 'github' });
    expect(answerParams(card, 'declined')).toMatchObject({ taskId: 't1' });

    // Our own copy names the service and the tool; the gateway's agent-facing
    // prose is not rendered when there is a URL to link.
    const rendered = JSON.stringify(card);
    expect(rendered).toContain('github');
    expect(rendered).toContain('github_get_me');
    expect(rendered).not.toContain('You must tell the end-user');
  });

  test('with no URL anywhere, the wire text is shown and the answers stay', () => {
    const prompt = readAuthRequired([textPart('Authorization is required to continue.')], undefined);
    const card = service.buildInTaskAuthCard(config, prompt, { taskId: 't1' });

    expect(buttons(card).find((b: any) => b.onClick?.openLink)).toBeUndefined();
    expect(JSON.stringify(card)).toContain('Authorization is required to continue.');
    expect(answerParams(card, 'approved')).toBeDefined();
    expect(answerParams(card, 'declined')).toBeDefined();
  });
});

describe('the answer sent back', () => {
  test('the DataPart is what the executor routes to the parked interrupt', () => {
    expect(authorizationDataPart('approved')).toEqual({ authorization: { decision: 'approved' } });
    expect(authorizationDataPart('declined')).toEqual({ authorization: { decision: 'declined' } });
  });

  test('the prose beside it names the tool and tells the agent what to do', () => {
    expect(authResumeText('approved', 'github_get_me')).toContain('github_get_me');
    expect(authResumeText('declined')).toContain('Do not ask again');
  });
});
