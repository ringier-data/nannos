import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { BrokerUserAuthService } from '../../src/services/brokerUserAuthService.js';
import { CompositeUserAuthService } from '../../src/services/compositeUserAuthService.js';
import { BrokerClient, BrokerError, BrokerSignInRequiredError } from '../../src/services/brokerClient.js';
import type { IUserAuthService } from '../../src/services/userAuthService.js';
import { Config } from '../../src/config/config.js';
import type { Storage, UserAuthToken } from '../../src/storage/storage.js';

/** An in-memory user_auth table keyed by email, like the real one. */
class MemoryStorage {
  rows = new Map<string, UserAuthToken>();
  saveToken = jest.fn(async (token: Omit<UserAuthToken, 'createdAt' | 'updatedAt'>) => {
    this.rows.set(token.email, { ...token, createdAt: 0, updatedAt: 0 });
  });
  getToken = jest.fn(async (email: string) => this.rows.get(email) ?? null);
  deleteToken = jest.fn(async (email: string) => {
    this.rows.delete(email);
  });
  saveOAuthState = jest.fn(async () => undefined);
}

const config = {
  baseUrl: 'https://email.example',
  oidc: { orchestratorAudience: 'orchestrator' },
} as unknown as Config;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMock = jest.Mock<(...args: any[]) => any>;

describe('BrokerUserAuthService (email)', () => {
  let storage: MemoryStorage;
  let broker: { authorizeUrl: AnyMock; redeem: AnyMock; mint: AnyMock };
  let service: BrokerUserAuthService;

  beforeEach(() => {
    storage = new MemoryStorage();
    broker = {
      authorizeUrl: jest.fn((redirectUri: string, state: string) => `https://console/authorize?r=${redirectUri}&s=${state}`),
      redeem: jest.fn(async () => ({ user_id: 'u1', sub: 'sub-1', groups: [] })),
      mint: jest.fn(async (_sub: string, audience: string) => ({
        accessToken: `token-for-${audience}`,
        expiresAt: Date.now() + 3600_000,
      })),
    };
    service = new BrokerUserAuthService(storage as unknown as Storage, broker as unknown as BrokerClient, config);
  });

  const signIn = () =>
    service.completeOAuthFlow(
      'ada@example.com',
      'https://email.example/api/v1/oauth/callback?code=code-1&state=s1',
      'unused-verifier',
      's1'
    );

  test('the sign-in link goes to the broker and comes back to the usual callback', async () => {
    await service.getAuthorizationUrl('s1', 'verifier');
    expect(broker.authorizeUrl).toHaveBeenCalledWith('https://email.example/api/v1/oauth/callback', 's1');
  });

  test('completing the sign-in stores only who the sender is', async () => {
    await signIn();
    expect(broker.redeem).toHaveBeenCalledWith('code-1');
    expect(storage.rows.get('ada@example.com')).toMatchObject({ oidcSub: 'sub-1', authMode: 'broker' });
    expect(storage.rows.get('ada@example.com')?.accessToken).toBeUndefined();
    expect(await service.isUserAuthorized('ada@example.com')).toBe(true);
  });

  test('tokens are minted per audience and cached', async () => {
    await signIn();
    expect(await service.getOrchestratorToken('ada@example.com')).toBe('token-for-orchestrator');
    expect(await service.getOrchestratorToken('ada@example.com')).toBe('token-for-orchestrator');
    expect(broker.mint).toHaveBeenCalledTimes(1);
  });

  test('when the broker says sign in again, the sign-in is forgotten', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw new BrokerSignInRequiredError('HTTP 409');
    });
    expect(await service.getOrchestratorToken('ada@example.com')).toBeNull();
    expect(await service.isUserAuthorized('ada@example.com')).toBe(false);
  });

  test('a local row is not a broker sign-in', async () => {
    storage.rows.set('ada@example.com', { email: 'ada@example.com', authMode: 'local', createdAt: 0, updatedAt: 0 });
    expect(await service.isUserAuthorized('ada@example.com')).toBe(false);
    expect(await service.getOrchestratorToken('ada@example.com')).toBeNull();
  });

  test.each([
    ['an audience this client may not have', new BrokerError('HTTP 403', 403)],
    ['Keycloak down', new BrokerError('HTTP 502', 502)],
    ['the broker unreachable', new TypeError('fetch failed')],
  ])('%s is thrown, not answered with "sign in again", and keeps the sign-in', async (_case, failure) => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw failure;
    });

    // null would make the caller ask for a sign-in, which cannot fix any of these.
    await expect(service.getOrchestratorToken('ada@example.com')).rejects.toBe(failure);
    expect(storage.rows.has('ada@example.com')).toBe(true);
    expect(await service.isUserAuthorized('ada@example.com')).toBe(true);
  });
});

describe('CompositeUserAuthService (email drain)', () => {
  function fake(name: string): IUserAuthService & Record<string, AnyMock> {
    return {
      isUserAuthorized: jest.fn(async () => true),
      getTokenForAudience: jest.fn(async () => `${name}-token`),
      getOrchestratorToken: jest.fn(async () => `${name}-orchestrator-token`),
      completeOAuthFlow: jest.fn(async () => undefined),
      getAuthorizationUrl: jest.fn(async () => `${name}-url`),
      storeAuthState: jest.fn(async () => undefined),
    } as unknown as IUserAuthService & Record<string, AnyMock>;
  }

  test('local rows with a refresh token stay local; sign-ins and everything else go to the broker', async () => {
    const storage = new MemoryStorage();
    const local = fake('local');
    const broker = fake('broker');
    const service = new CompositeUserAuthService(storage as unknown as Storage, local, broker);

    storage.rows.set('old@example.com', {
      email: 'old@example.com',
      refreshToken: 'rt',
      authMode: 'local',
      createdAt: 0,
      updatedAt: 0,
    });
    expect(await service.getOrchestratorToken('old@example.com')).toBe('local-orchestrator-token');
    expect(await service.getOrchestratorToken('new@example.com')).toBe('broker-orchestrator-token');
    expect(await service.getAuthorizationUrl('s', 'v')).toBe('broker-url');
    await service.completeOAuthFlow('old@example.com', 'https://x/cb?code=c', 'v', 's');
    expect(broker.completeOAuthFlow).toHaveBeenCalled();
    expect(local.completeOAuthFlow).not.toHaveBeenCalled();
    // Signed in through the broker now, although the (fake) local row is still there.
    expect(await service.getOrchestratorToken('old@example.com')).toBe('broker-orchestrator-token');
  });

  test('a sender the broker serves is not looked up again on every call', async () => {
    const storage = new MemoryStorage();
    const service = new CompositeUserAuthService(storage as unknown as Storage, fake('local'), fake('broker'));
    storage.rows.set('ada@example.com', { email: 'ada@example.com', oidcSub: 's', authMode: 'broker', createdAt: 0, updatedAt: 0 });
    const getToken = jest.spyOn(storage, 'getToken');

    await service.isUserAuthorized('ada@example.com');
    await service.getOrchestratorToken('ada@example.com');

    expect(getToken).toHaveBeenCalledTimes(1);
  });
});
