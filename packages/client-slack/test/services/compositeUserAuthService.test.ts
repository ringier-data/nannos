import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { CompositeUserAuthService } from '../../src/services/compositeUserAuthService.js';
import type { IUserAuthService } from '../../src/services/userAuthService.js';
import type { IUserAuthStorage, UserAuthToken } from '../../src/storage/types.js';

function fakeService(name: string): IUserAuthService & Record<string, jest.Mock> {
  return {
    isUserAuthorized: jest.fn(async () => true),
    getTokenForAudience: jest.fn(async () => `${name}-token`),
    getOrchestratorToken: jest.fn(async () => `${name}-orchestrator-token`),
    completeOAuthFlow: jest.fn(async () => ({}) as UserAuthToken),
    revokeUserAuthorization: jest.fn(async () => undefined),
    getAuthorizationUrl: jest.fn(async () => `${name}-url`),
    storeAuthState: jest.fn(async () => undefined),
  } as unknown as IUserAuthService & Record<string, jest.Mock>;
}

describe('CompositeUserAuthService (drain)', () => {
  let row: UserAuthToken | null;
  let local: ReturnType<typeof fakeService>;
  let broker: ReturnType<typeof fakeService>;
  let service: CompositeUserAuthService;

  beforeEach(() => {
    row = null;
    local = fakeService('local');
    broker = fakeService('broker');
    const storage = { getToken: jest.fn(async () => row) } as unknown as IUserAuthStorage;
    service = new CompositeUserAuthService(storage, local, broker);
  });

  test('a user who signed in the old way keeps being served locally', async () => {
    row = { userId: 'U1', teamId: 'T1', refreshToken: 'rt', authMode: 'local', createdAt: 0, updatedAt: 0 };

    expect(await service.getOrchestratorToken('U1', 'T1')).toBe('local-orchestrator-token');
    expect(await service.getTokenForAudience('U1', 'T1', 'agent-console')).toBe('local-token');
    expect(broker.getOrchestratorToken).not.toHaveBeenCalled();
  });

  test('a broker row, a row without a refresh token, and no row all go to the broker', async () => {
    for (const current of [
      { userId: 'U1', teamId: 'T1', oidcSub: 's', authMode: 'broker' as const, createdAt: 0, updatedAt: 0 },
      { userId: 'U1', teamId: 'T1', accessToken: 'at', authMode: 'local' as const, createdAt: 0, updatedAt: 0 },
      null,
    ]) {
      row = current;
      expect(await service.isUserAuthorized('U1', 'T1')).toBe(true);
    }
    expect(broker.isUserAuthorized).toHaveBeenCalledTimes(3);
    expect(local.isUserAuthorized).not.toHaveBeenCalled();
  });

  test('every new sign-in goes through the broker, even for a local user', async () => {
    row = { userId: 'U1', teamId: 'T1', refreshToken: 'rt', authMode: 'local', createdAt: 0, updatedAt: 0 };

    expect(await service.getAuthorizationUrl('s', 'T1', 'v')).toBe('broker-url');
    await service.storeAuthState('s', 'U1', 'T1');
    await service.completeOAuthFlow('U1', 'T1', 'https://x/cb?code=c', 'v', 's');

    expect(broker.storeAuthState).toHaveBeenCalled();
    expect(broker.completeOAuthFlow).toHaveBeenCalled();
    expect(local.completeOAuthFlow).not.toHaveBeenCalled();
  });
});
