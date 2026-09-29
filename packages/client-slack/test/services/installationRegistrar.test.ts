import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { registerInstallations } from '../../src/services/installationRegistrar.js';
import { Config } from '../../src/config/config.js';
import { OIDCClient } from '../../src/services/oidcClient.js';
import { InstallationSecretService } from '../../src/services/installationSecretService.js';
import { IBotInstallationStore } from '../../src/storage/types.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function installation(appId: string, teamId: string, botName: string) {
  return {
    appId,
    teamId,
    botName,
    botToken: 'xoxb-test',
    signingSecret: 'sig',
    hasAvatar: false,
    slashCommand: '/nannos',
    isActive: true,
    createdAt: new Date(),
    updatedAt: new Date(),
  };
}

/** Secrets keyed by whatever installation id the registrar asks for. */
class FakeSecretService extends InstallationSecretService {
  protected async resolve(installationId: string): Promise<string> {
    return `secret-for-${installationId}`;
  }
  protected async read(installationId: string): Promise<string | null> {
    return `secret-for-${installationId}`;
  }
}

function deps(bots: ReturnType<typeof installation>[]) {
  return {
    config: {
      consoleBackend: { url: 'https://console.example.com', audience: 'console-aud' },
      baseUrl: 'https://slack.example.com',
    } as unknown as Config,
    oidcClient: {
      getServiceToken: jest.fn<(a: string) => Promise<string>>().mockResolvedValue('svc-token'),
    } as unknown as OIDCClient,
    botInstallationStore: {
      listAll: jest.fn<() => Promise<ReturnType<typeof installation>[]>>().mockResolvedValue(bots),
    } as unknown as IBotInstallationStore,
    installationSecretService: new FakeSecretService(),
  };
}

function bodiesFrom(fetchMock: jest.Mock): Record<string, unknown>[] {
  return fetchMock.mock.calls.map((call) => JSON.parse((call[1] as { body: string }).body));
}

describe('registerInstallations', () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn(async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => '',
    })) as unknown as jest.Mock;
    (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
  });

  test('keys the channel on app_id, not the display name', async () => {
    await registerInstallations(deps([installation('A00000000AA', 'T000000AA', 'Nannos')]));

    const [body] = bodiesFrom(fetchMock);
    expect(body.installation_id).toBe('A00000000AA');
    // The human-readable label still names the workspace — it just isn't the key any more.
    expect(body.name).toBe('Slack Nannos (T000000AA)');
  });

  test('two workspaces sharing a bot name get distinct channels and distinct secrets', async () => {
    await registerInstallations(
      deps([
        installation('A00000000AA', 'T000000AA', 'Nannos'),
        installation('A11111111BB', 'T111111BB', 'Nannos'),
      ])
    );

    const bodies = bodiesFrom(fetchMock);
    expect(bodies).toHaveLength(2);
    expect(bodies.map((b) => b.installation_id)).toEqual(['A00000000AA', 'A11111111BB']);
    // The bug this replaces: one shared secret meant an inbound notification matched
    // whichever installation was listed first, so it landed in the wrong workspace.
    expect(new Set(bodies.map((b) => b.secret)).size).toBe(2);
  });

  test('one failing registration does not stop the rest', async () => {
    fetchMock
      .mockImplementationOnce(async () => ({ ok: false, status: 500, statusText: 'Boom', text: async () => 'boom' }))
      .mockImplementationOnce(async () => ({ ok: true, status: 201, statusText: 'Created', text: async () => '' }));

    const d = deps([
      installation('A00000000AA', 'T000000AA', 'Nannos'),
      installation('A22222222CC', 'T222222CC', 'Ada'),
    ]);
    await expect(registerInstallations(d)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test('each channel names its team as the workspace a sign-in reaches it through', async () => {
    await registerInstallations(
      deps([installation('A00000000AA', 'T000000AA', 'Nannos'), installation('A00000000AB', 'T000000AA', 'Dev')])
    );

    // One sign-in per team reaches every app installed there.
    expect(bodiesFrom(fetchMock).map((b) => [b.installation_id, b.workspace_id])).toEqual([
      ['A00000000AA', 'T000000AA'],
      ['A00000000AB', 'T000000AA'],
    ]);
  });

  test("a deactivated app has its existing channel's workspace cleared, update only", async () => {
    const gone = { ...installation('A0000000OLD', 'T000000AA', 'Old'), isActive: false };
    // Deactivated, and its channel deleted by an admin (or never created): no row to update.
    const deleted = { ...installation('A0000000DEL', 'T000000AA', 'Deleted'), isActive: false };
    fetchMock.mockImplementation(async (url: unknown, init: unknown) => {
      const listing = String(url).endsWith('/api/v1/delivery-channels') && !(init as { method?: string }).method;
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        text: async () => '',
        json: async () =>
          listing
            ? {
                channels: [
                  { id: 5, installation_id: 'A0000000OLD', workspace_id: 'T000000AA' },
                  { id: 6, installation_id: 'A00000000AA', workspace_id: 'T000000AA' },
                ],
              }
            : {},
      };
    });

    await registerInstallations(deps([installation('A00000000AA', 'T000000AA', 'Nannos'), gone, deleted]));

    const calls = fetchMock.mock.calls.map(([url, init]) => [
      (init as { method?: string }).method ?? 'GET',
      String(url).replace('https://console.example.com', ''),
      (init as { body?: string }).body ? JSON.parse((init as { body: string }).body) : undefined,
    ]);
    expect(calls.filter(([method]) => method !== 'POST')).toEqual([
      ['GET', '/api/v1/delivery-channels', undefined],
      ['PATCH', '/api/v1/delivery-channels/5', { workspace_id: null }],
    ]);
    // Registration (create-or-update) is for active apps only: nothing deactivated comes back.
    expect(calls.filter(([method]) => method === 'POST').map(([, , body]) => body.installation_id)).toEqual([
      'A00000000AA',
    ]);
  });
});
