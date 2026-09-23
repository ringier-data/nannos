import type { IUserAuthStorage, UserAuthToken } from '../storage/types.js';
import type { IUserAuthService } from './userAuthService.js';

/**
 * Broker mode while old sign-ins drain.
 *
 * New sign-ins go through the broker. A user who signed in the old way keeps being served
 * by the local implementation from their stored refresh token, until it lapses or they
 * sign in again, at which point their row becomes a broker row. Once no row holds a
 * refresh token any more, the factory can return the broker implementation alone.
 */
export class CompositeUserAuthService implements IUserAuthService {
  constructor(
    private readonly storage: IUserAuthStorage,
    private readonly local: IUserAuthService,
    private readonly broker: IUserAuthService
  ) {}

  private async forUser(userId: string, teamId: string): Promise<IUserAuthService> {
    const row = await this.storage.getToken(userId, teamId);
    return row && row.authMode !== 'broker' && row.refreshToken ? this.local : this.broker;
  }

  async isUserAuthorized(userId: string, teamId: string): Promise<boolean> {
    return (await this.forUser(userId, teamId)).isUserAuthorized(userId, teamId);
  }

  async getTokenForAudience(userId: string, teamId: string, audience: string): Promise<string | null> {
    return (await this.forUser(userId, teamId)).getTokenForAudience(userId, teamId, audience);
  }

  async getOrchestratorToken(userId: string, teamId: string): Promise<string | null> {
    return (await this.forUser(userId, teamId)).getOrchestratorToken(userId, teamId);
  }

  async revokeUserAuthorization(userId: string, teamId: string): Promise<void> {
    return (await this.forUser(userId, teamId)).revokeUserAuthorization(userId, teamId);
  }

  // Signing in is always the broker's.

  completeOAuthFlow(
    userId: string,
    teamId: string,
    callbackUrl: string,
    codeVerifier: string,
    state: string
  ): Promise<UserAuthToken> {
    return this.broker.completeOAuthFlow(userId, teamId, callbackUrl, codeVerifier, state);
  }

  getAuthorizationUrl(state: string, teamId: string, codeVerifier: string): Promise<string> {
    return this.broker.getAuthorizationUrl(state, teamId, codeVerifier);
  }

  storeAuthState(state: string, userId: string, teamId: string): Promise<void> {
    return this.broker.storeAuthState(state, userId, teamId);
  }
}
