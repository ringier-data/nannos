import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { registerInstallations } from '../../src/services/installationRegistrar.js';
import { Config } from '../../src/config/config.js';
import { OIDCClient } from '../../src/services/oidcClient.js';
import { InstallationSecretService } from '../../src/services/installationSecretService.js';
import type { BrokerClient } from '../../src/services/brokerClient.js';

/** Secrets keyed by whatever installation id the registrar asks for. */
class FakeSecretService extends InstallationSecretService {
  protected async resolve(installationId: string): Promise<string> {
    return `secret-for-${installationId}`;
  }
  protected async read(installationId: string): Promise<string | null> {
    return `secret-for-${installationId}`;
  }
}

function deps(broker?: BrokerClient, publishRetryDelaysMs?: number[]) {
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
    broker,
    publishRetryDelaysMs,
  };
}

describe('registerInstallations', () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn(async () => ({ ok: true, status: 200, statusText: 'OK', text: async () => '' })) as unknown as jest.Mock;
    (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
  });

  test('without the broker it registers the channels and publishes nothing', async () => {
    await registerInstallations(deps());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test('in broker mode each project number is published with the name its channel is registered under', async () => {
    const setWorkspaceInstallations = jest.fn<(workspaceId: string, ids: string[]) => Promise<void>>().mockResolvedValue();

    await registerInstallations(deps({ setWorkspaceInstallations } as unknown as BrokerClient));

    // A sign-in is keyed by the project number; its delivery channel by the project name.
    expect(setWorkspaceInstallations.mock.calls).toEqual([
      ['111', ['chat-project']],
      ['222', ['other-project']],
    ]);
  });

  test('a refused project is retried without holding up the other', async () => {
    const setWorkspaceInstallations = jest
      .fn<(workspaceId: string, ids: string[]) => Promise<void>>()
      .mockRejectedValueOnce(new Error('403'))
      .mockResolvedValue();

    await registerInstallations(deps({ setWorkspaceInstallations } as unknown as BrokerClient, [0]));

    expect(setWorkspaceInstallations.mock.calls.map(([id]) => id)).toEqual(['111', '222', '111']);
  });
});
