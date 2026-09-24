import type { Storage } from '../storage/storage.js';
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
  /**
   * Senders the broker serves. A sender never goes back to the local login, because every
   * sign-in here is the broker's. So their row is not read again just to pick the
   * implementation, which then reads it once more.
   */
  private readonly brokerUsers = new Set<string>();

  constructor(
    private readonly storage: Pick<Storage, 'getToken'>,
    private readonly local: IUserAuthService,
    private readonly broker: IUserAuthService
  ) {}

  private async forUser(email: string): Promise<IUserAuthService> {
    if (this.brokerUsers.has(email)) {
      return this.broker;
    }
    const row = await this.storage.getToken(email);
    if (row && row.authMode !== 'broker' && row.refreshToken) {
      return this.local;
    }
    this.brokerUsers.add(email);
    return this.broker;
  }

  async isUserAuthorized(email: string): Promise<boolean> {
    return (await this.forUser(email)).isUserAuthorized(email);
  }

  async getTokenForAudience(email: string, audience: string): Promise<string | null> {
    return (await this.forUser(email)).getTokenForAudience(email, audience);
  }

  async getOrchestratorToken(email: string): Promise<string | null> {
    return (await this.forUser(email)).getOrchestratorToken(email);
  }

  // Signing in is always the broker's.

  async completeOAuthFlow(email: string, callbackUrl: string, codeVerifier: string, state: string): Promise<void> {
    await this.broker.completeOAuthFlow(email, callbackUrl, codeVerifier, state);
    this.brokerUsers.add(email);
  }

  getAuthorizationUrl(state: string, codeVerifier: string): Promise<string> {
    return this.broker.getAuthorizationUrl(state, codeVerifier);
  }

  storeAuthState(state: string, email: string, codeVerifier: string): Promise<void> {
    return this.broker.storeAuthState(state, email, codeVerifier);
  }
}
