import type { IUserAuthStorage, IOAuthStateStore, UserAuthToken } from '../storage/types.js';
import { OIDCClient } from './oidcClient.js';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';

/**
 * Cached audience-specific token
 */
interface CachedAudienceToken {
  accessToken: string;
  expiresAt: number;
}

/**
 * Service to manage user authentication and OIDC tokens
 */
export class UserAuthService {
  private readonly storage: IUserAuthStorage;
  private readonly oidcClient: OIDCClient;
  private readonly config: Config;
  private readonly oauthStateStore: IOAuthStateStore;
  private readonly logger = Logger.getLogger(UserAuthService.name);

  /**
   * In-memory cache for audience-specific tokens
   * Key format: `${userId}:${projectId}:${audience}`
   */
  private readonly audienceTokenCache = new Map<string, CachedAudienceToken>();

  constructor(storage: IUserAuthStorage, oidcClient: OIDCClient, config: Config, oauthStateStore: IOAuthStateStore) {
    this.storage = storage;
    this.oidcClient = oidcClient;
    this.config = config;
    this.oauthStateStore = oauthStateStore;
  }

  /**
   * Clear cached audience tokens for a user (called when base token is refreshed)
   */
  private clearAudienceTokenCache(userId: string, projectId: string): void {
    const prefix = `${userId}:${projectId}`;
    for (const key of this.audienceTokenCache.keys()) {
      if (key.startsWith(prefix)) {
        this.audienceTokenCache.delete(key);
        this.logger.debug(`Cleared cached audience token: ${key}`);
      }
    }
  }

  /**
   * Check if user is authorized (has valid token, refreshing if needed)
   */
  async isUserAuthorized(userId: string, projectId: string): Promise<boolean> {
    // Try to get a valid access token (this will refresh if needed)
    const accessToken = await this.getUserAccessToken(userId, projectId);
    return accessToken !== null;
  }

  /**
   * Get user's access token, refreshing if necessary
   */
  async getUserAccessToken(userId: string, projectId: string): Promise<string | null> {
    const token = await this.storage.getToken(userId, projectId);

    if (!token) {
      this.logger.debug(`No token found for user ${userId} in project ${projectId}`);
      return null;
    }

    // Check if token is expired (with 5 minute buffer)
    const now = Date.now();
    const bufferMs = 5 * 60 * 1000; // 5 minutes

    if (token.expiresAt > now + bufferMs) {
      // Token is still valid
      this.logger.debug(
        `Token for user ${userId} is still valid (expires in ${Math.round((token.expiresAt - now) / 1000)}s)`
      );
      return token.accessToken;
    }

    // Token is expired or about to expire, try to refresh
    this.logger.info(`Token for user ${userId} expired or expiring soon, attempting refresh`);

    if (!token.refreshToken) {
      this.logger.info(`Token expired and no refresh token available for user ${userId}`);
      return null;
    }

    try {
      this.logger.info(`Refreshing token for user ${userId} in project ${projectId}`);
      const refreshedToken = await this.oidcClient.refreshAccessToken(token.refreshToken);

      this.logger.info(
        `Token refreshed successfully for user ${userId}, new expiry in ${Math.round((refreshedToken.expiresAt - Date.now()) / 1000)}s`
      );

      // Clear cached audience-specific tokens since base token changed
      this.clearAudienceTokenCache(userId, projectId);

      // Update stored token
      await this.storage.updateToken(userId, projectId, {
        accessToken: refreshedToken.accessToken,
        refreshToken: refreshedToken.refreshToken,
        expiresAt: refreshedToken.expiresAt,
        idToken: refreshedToken.idToken,
      });

      return refreshedToken.accessToken;
    } catch (error: any) {
      this.logger.error(error, `Failed to refresh token for user ${userId}: ${error}`);

      // If refresh token is invalid/expired, delete the stored token so user can re-authorize cleanly
      if (error?.error === 'invalid_grant' || error?.cause?.error === 'invalid_grant') {
        this.logger.info(`Refresh token invalid for user ${userId}, deleting stored token`);
        await this.storage
          .deleteToken(userId, projectId)
          .catch((e) => this.logger.debug(`Failed to delete invalid token: ${e}`));
      }

      // Token refresh failed, user needs to re-authorize
      return null;
    }
  }

  /**
   * Get user's access token for a specific audience (e.g., 'orchestrator')
   * Uses RFC 8693 token exchange and caches the result
   */
  async getTokenForAudience(userId: string, projectId: string, audience: string): Promise<string | null> {
    // First, get a valid base access token
    const baseAccessToken = await this.getUserAccessToken(userId, projectId);
    if (!baseAccessToken) {
      return null;
    }

    const cacheKey = `${userId}:${projectId}:${audience}`;
    const now = Date.now();
    const bufferMs = 5 * 60 * 1000; // 5 minutes buffer

    // Check if we have a valid cached token for this audience
    const cachedToken = this.audienceTokenCache.get(cacheKey);
    if (cachedToken && cachedToken.expiresAt > now + bufferMs) {
      this.logger.debug(
        `Using cached ${audience} token for user ${userId} (expires in ${Math.round((cachedToken.expiresAt - now) / 1000)}s)`
      );
      return cachedToken.accessToken;
    }

    // Exchange the base token for an audience-specific token
    try {
      this.logger.info(`Exchanging token for audience '${audience}' for user ${userId}`);
      const exchangedToken = await this.oidcClient.exchangeTokenForAudience(baseAccessToken, audience);

      // Cache the exchanged token
      this.audienceTokenCache.set(cacheKey, {
        accessToken: exchangedToken.accessToken,
        expiresAt: exchangedToken.expiresAt,
      });

      this.logger.info(
        `Token exchange successful for user ${userId}, audience '${audience}', expires in ${Math.round((exchangedToken.expiresAt - now) / 1000)}s`
      );

      return exchangedToken.accessToken;
    } catch (error) {
      this.logger.error(error, `Failed to exchange token for audience '${audience}' for user ${userId}`);
      return null;
    }
  }

  /**
   * Get user's access token for the orchestrator audience
   * Convenience method that uses the configured orchestrator audience
   */
  async getOrchestratorToken(userId: string, projectId: string): Promise<string | null> {
    return this.getTokenForAudience(userId, projectId, this.config.oidc.orchestratorAudience);
  }

  /**
   * Complete OAuth flow by exchanging code for tokens
   */
  async completeOAuthFlow(
    userId: string,
    projectId: string,
    callbackUrl: string,
    codeVerifier: string,
    state: string
  ): Promise<UserAuthToken> {
    try {
      this.logger.info(`Completing OAuth flow for user ${userId} in project ${projectId}`);

      const tokens = await this.oidcClient.exchangeCodeForTokens(callbackUrl, codeVerifier, state);

      const userAuthToken: UserAuthToken = {
        userId,
        projectId,
        ...tokens,
      };

      await this.storage.saveToken(userAuthToken);

      this.logger.info(`Successfully completed OAuth flow for user ${userId}`);

      return userAuthToken;
    } catch (error) {
      this.logger.error(error, `Failed to complete OAuth flow for user ${userId}: ${error}`);
      throw error;
    }
  }

  /**
   * Revoke user's authorization by deleting their tokens
   */
  async revokeUserAuthorization(userId: string, projectId: string): Promise<void> {
    this.logger.info(`Revoking authorization for user ${userId} in project ${projectId}`);
    await this.storage.deleteToken(userId, projectId);
  }

  /**
   * Get authorization URL for user to start OAuth flow
   */
  async getAuthorizationUrl(state: string, _projectId: string, codeVerifier: string): Promise<string> {
    return this.oidcClient.getAuthorizationUrl(state, codeVerifier);
  }

  /**
   * Store authorization state for later retrieval
   */
  async storeAuthState(state: string, userId: string, projectId: string): Promise<void> {
    const oidc = await import('openid-client');
    const codeVerifier = oidc.randomPKCECodeVerifier();

    this.oauthStateStore.set(state, userId, projectId, codeVerifier, 604800); // 7 day TTL
  }
}
