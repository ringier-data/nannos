import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { registerInstallations } from '../../src/services/installationRegistrar.js';
import { Config } from '../../src/config/config.js';
import { OIDCClient } from '../../src/services/oidcClient.js';
import { InstallationSecretService } from '../../src/services/installationSecretService.js';

/** Secrets keyed by whatever installation id the registrar asks for. */
class FakeSecretService extends InstallationSecretService {
  protected async resolve(installationId: string): Promise<string> {
    return `secret-for-${installationId}`;
  }
  protected async read(installationId: string): Promise<string | null> {
    return `secret-for-${installationId}`;
  }
}

function deps() {
  return {
    config: {
      consoleBackend: { url: 'https://console.example.com', audience: 'console-aud' },
      baseUrl: 'https://gchat.example.com',
      googleChatConfigs: [
        { projectName: 'chat-project', projectNumber: '111', botName: 'Nannos' },
        { projectName: 'other-project', projectNumber: '222', botName: 'Nannos' },
      ],
    } as unknown as Config,
    oidcClient: {
      getServiceToken: jest.fn<(a: string) => Promise<string>>().mockResolvedValue('svc-token'),
    } as unknown as OIDCClient,
    installationSecretService: new FakeSecretService(),
  };
}

describe('registerInstallations', () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn(async () => ({ ok: true, status: 200, statusText: 'OK', text: async () => '' })) as unknown as jest.Mock;
    (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
  });

  test('each channel names its project number as the workspace a sign-in reaches it through', async () => {
    await registerInstallations(deps());

    // A sign-in is keyed by the project number; the channel by the project name.
    const bodies = fetchMock.mock.calls.map((call) => JSON.parse((call[1] as { body: string }).body));
    expect(bodies.map((b) => [b.installation_id, b.workspace_id])).toEqual([
      ['chat-project', '111'],
      ['other-project', '222'],
    ]);
  });
});
