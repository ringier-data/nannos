import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { BrokerUserAuthService } from '../../src/services/brokerUserAuthService.js';
import { BrokerClient, BrokerSignInRequiredError } from '../../src/services/brokerClient.js';
import { Config } from '../../src/config/config.js';
import type { IOAuthStateStore, IUserAuthStorage, UserAuthToken } from '../../src/storage/types.js';

/** An in-memory user_auth table keyed like the real one. */
class MemoryUserAuthStorage {
  rows = new Map<string, UserAuthToken>();
  key = (userId: string, teamId: string) => `${userId}:${teamId}`;
  saveToken = jest.fn(async (token: UserAuthToken) => {
    this.rows.set(this.key(token.userId, token.teamId), token);
  });
  getToken = jest.fn(async (userId: string, teamId: string) => this.rows.get(this.key(userId, teamId)) ?? null);
  deleteToken = jest.fn(async (userId: string, teamId: string) => {
    this.rows.delete(this.key(userId, teamId));
  });
}

const config = {
  baseUrl: 'https://slack.example',
  oidc: { orchestratorAudience: 'orchestrator' },
} as unknown as Config;

describe('BrokerUserAuthService', () => {
  let storage: MemoryUserAuthStorage;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type AnyMock = jest.Mock<(...args: any[]) => any>;
  let broker: { authorizeUrl: AnyMock; redeem: AnyMock; mint: AnyMock };
  let service: BrokerUserAuthService;

  beforeEach(() => {
    storage = new MemoryUserAuthStorage();
    broker = {
      authorizeUrl: jest.fn((redirectUri: string, state: string) => `https://console/authorize?r=${redirectUri}&s=${state}`),
      redeem: jest.fn(async () => ({ user_id: 'u1', sub: 'sub-1', groups: [] })),
      mint: jest.fn(async (_sub: string, audience: string) => ({
        accessToken: `token-for-${audience}`,
        expiresAt: Date.now() + 3600_000,
      })),
    };
    service = new BrokerUserAuthService(
      storage as unknown as IUserAuthStorage,
      broker as unknown as BrokerClient,
      config,
      { set: jest.fn() } as unknown as IOAuthStateStore
    );
  });

  async function signIn() {
    return service.completeOAuthFlow(
      'U1',
      'T1',
      'https://slack.example/api/v1/oauth/callback?code=code-1&state=s1',
      'unused-verifier',
      's1'
    );
  }

  test('the sign-in link goes to the broker and comes back to the usual callback', async () => {
    const url = await service.getAuthorizationUrl('s1', 'T1', 'verifier');
    expect(broker.authorizeUrl).toHaveBeenCalledWith('https://slack.example/api/v1/oauth/callback', 's1');
    expect(url).toContain('https://console/authorize');
  });

  test('completing the sign-in stores only who the user is', async () => {
    const row = await signIn();

    expect(broker.redeem).toHaveBeenCalledWith('code-1');
    expect(row).toMatchObject({ userId: 'U1', teamId: 'T1', oidcSub: 'sub-1', authMode: 'broker' });
    expect(row.accessToken).toBeUndefined();
    expect(row.refreshToken).toBeUndefined();
    expect(await service.isUserAuthorized('U1', 'T1')).toBe(true);
  });

  test('a callback without a code fails', async () => {
    await expect(
      service.completeOAuthFlow('U1', 'T1', 'https://slack.example/api/v1/oauth/callback?state=s1', 'v', 's1')
    ).rejects.toThrow('no code');
  });

  test('a local row is not a broker sign-in', async () => {
    storage.rows.set('U1:T1', { userId: 'U1', teamId: 'T1', oidcSub: 'sub-1', authMode: 'local', createdAt: 0, updatedAt: 0 });
    expect(await service.isUserAuthorized('U1', 'T1')).toBe(false);
    expect(await service.getOrchestratorToken('U1', 'T1')).toBeNull();
    expect(broker.mint).not.toHaveBeenCalled();
  });

  test('tokens are minted per audience and cached', async () => {
    await signIn();

    expect(await service.getOrchestratorToken('U1', 'T1')).toBe('token-for-orchestrator');
    expect(await service.getOrchestratorToken('U1', 'T1')).toBe('token-for-orchestrator');
    expect(await service.getTokenForAudience('U1', 'T1', 'agent-console')).toBe('token-for-agent-console');

    expect(broker.mint.mock.calls).toEqual([
      ['sub-1', 'orchestrator'],
      ['sub-1', 'agent-console'],
    ]);
  });

  test('a token close to its expiry is minted again', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => ({ accessToken: 'short', expiresAt: Date.now() + 1000 }));

    await service.getOrchestratorToken('U1', 'T1');
    await new Promise((resolve) => setTimeout(resolve, 600)); // past half the lifetime
    await service.getOrchestratorToken('U1', 'T1');

    expect(broker.mint).toHaveBeenCalledTimes(2);
  });

  test('when the broker says sign in again, the sign-in is forgotten', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw new BrokerSignInRequiredError('HTTP 409');
    });

    expect(await service.getOrchestratorToken('U1', 'T1')).toBeNull();
    expect(storage.deleteToken).toHaveBeenCalledWith('U1', 'T1');
    expect(await service.isUserAuthorized('U1', 'T1')).toBe(false);
  });

  test('any other broker failure keeps the sign-in', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw new Error('network down');
    });

    expect(await service.getOrchestratorToken('U1', 'T1')).toBeNull();
    expect(storage.deleteToken).not.toHaveBeenCalled();
    expect(await service.isUserAuthorized('U1', 'T1')).toBe(true);
  });

  test('revoking forgets the sign-in and the cached tokens', async () => {
    await signIn();
    await service.getOrchestratorToken('U1', 'T1');

    await service.revokeUserAuthorization('U1', 'T1');

    expect(await service.getOrchestratorToken('U1', 'T1')).toBeNull();
    expect(broker.mint).toHaveBeenCalledTimes(1);
  });
});
