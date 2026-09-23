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
  constructor(
    private readonly storage: Pick<Storage, 'getToken'>,
    private readonly local: IUserAuthService,
    private readonly broker: IUserAuthService
  ) {}

  private async forUser(email: string): Promise<IUserAuthService> {
    const row = await this.storage.getToken(email);
    return row && row.authMode !== 'broker' && row.refreshToken ? this.local : this.broker;
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

  completeOAuthFlow(email: string, callbackUrl: string, codeVerifier: string, state: string): Promise<void> {
    return this.broker.completeOAuthFlow(email, callbackUrl, codeVerifier, state);
  }

  getAuthorizationUrl(state: string, codeVerifier: string): Promise<string> {
    return this.broker.getAuthorizationUrl(state, codeVerifier);
  }

  storeAuthState(state: string, email: string, codeVerifier: string): Promise<void> {
    return this.broker.storeAuthState(state, email, codeVerifier);
  }
}
