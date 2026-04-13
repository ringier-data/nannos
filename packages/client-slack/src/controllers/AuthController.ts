import crypto from 'crypto';
import { Context } from 'koa';
import { decodeJwt } from 'jose';
import * as client from 'openid-client';
import { Config } from '../config/config.js';
import { OIDCClient } from '../services/oidcClient.js';
import { Logger } from '../utils/logger.js';
import type { IAdminSessionStore } from '../storage/types.js';

interface StateCookie {
  codeVerifier: string;
  redirectTo: string;
}

export class AuthController {
  private readonly logger = Logger.getLogger('AuthController');
  private readonly isDevOrLocal: boolean;
  private readonly scopes = 'openid profile email';

  constructor(
    private readonly config: Config,
    private readonly sessionStore: IAdminSessionStore,
    private readonly oidcClient: OIDCClient
  ) {
    this.isDevOrLocal = config.isLocal() || config.isDev();
  }

  async getLogin(ctx: Context) {
    const redirectTo = (ctx.query.redirectTo as string) || '/';

    if (!this.isValidRedirectUrl(redirectTo)) {
      ctx.status = 400;
      ctx.body = { error: 'redirectTo must be a valid URL' };
      return;
    }

    const codeVerifier = client.randomPKCECodeVerifier();
    const state = crypto.randomBytes(16).toString('hex');

    const stateCookie: StateCookie = { codeVerifier, redirectTo };
    ctx.cookies.set(`auth-state-${state}`, JSON.stringify(stateCookie), {
      path: '/api/v2/auth/callback',
      sameSite: 'lax',
      httpOnly: true,
      signed: true,
      maxAge: 600_000, // 10 minutes
      secure: !this.config.isLocal(),
    });

    const callbackUrl = new URL('/api/v2/auth/callback', this.config.baseUrl).toString();
    const authorizationUrl = await this.oidcClient.getAuthorizationUrlV2(state, codeVerifier, callbackUrl, this.scopes);

    ctx.status = 303;
    ctx.redirect(authorizationUrl);
  }

  private redirectToError(ctx: Context, error: string, message: string) {
    const errorPageUrl = new URL('/auth/error', this.config.baseUrl);
    errorPageUrl.searchParams.set('error', error);
    errorPageUrl.searchParams.set('message', message);
    ctx.status = 303;
    ctx.redirect(errorPageUrl.toString());
  }

  async getLoginCallback(ctx: Context) {
    const state = ctx.query.state as string;
    if (!state) {
      return this.redirectToError(ctx, 'invalid_request', 'Missing state parameter');
    }

    const rawCookie = ctx.cookies.get(`auth-state-${state}`, { signed: true });
    if (!rawCookie) {
      return this.redirectToError(ctx, 'invalid_state', 'Invalid or expired state. Please try logging in again.');
    }

    let stateCookie: StateCookie;
    try {
      stateCookie = JSON.parse(rawCookie);
    } catch {
      return this.redirectToError(ctx, 'invalid_state', 'Corrupt state cookie. Please try logging in again.');
    }

    // Clear the state cookie immediately (one-time use)
    ctx.cookies.set(`auth-state-${state}`, '', { path: '/api/v2/auth/callback' });

    // Exchange authorization code for tokens
    // Rewrite the callback URL to use config.baseUrl so redirect_uri matches the one
    // sent during the authorization request (the frontend proxies /api to the backend).
    let tokens;
    try {
      const incomingUrl = new URL(ctx.href);
      const callbackUrl = new URL(incomingUrl.pathname + incomingUrl.search, this.config.baseUrl).toString();
      tokens = await this.oidcClient.exchangeCodeForTokensV2(callbackUrl, stateCookie.codeVerifier, state);
    } catch (err) {
      this.logger.error({ err }, 'OIDC token exchange failed');
      return this.redirectToError(ctx, 'token_exchange_failed', 'Failed to complete login. Please try again.');
    }

    if (!tokens.accessToken) {
      return this.redirectToError(ctx, 'no_access_token', 'Login provider did not return an access token.');
    }

    // Decode access token to extract groups claim
    const accessTokenPayload = decodeJwt(tokens.accessToken);
    const groups: string[] = (accessTokenPayload.groups as string[]) ?? [];
    const sub = accessTokenPayload.sub;
    const email = (accessTokenPayload.email as string) ?? undefined;

    if (!sub) {
      return this.redirectToError(ctx, 'invalid_token', 'Access token is missing required claims.');
    }

    // Check group membership
    if (!groups.includes(this.config.adminGroup)) {
      this.logger.warn(`User ${sub} (${email}) denied access: not in group ${this.config.adminGroup}`);
      return this.redirectToError(
        ctx,
        'access_denied',
        'You are not a member of the required admin group. Your groups: ' + groups.join(', ')
      );
    }

    // Create session
    const sessionId = crypto.randomBytes(32).toString('hex');
    const now = Date.now();
    await this.sessionStore.createSession({
      sessionId,
      sub,
      email,
      groups,
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
      accessTokenExpiresAt: tokens.expiresAt,
      createdAt: now,
      expiresAt: now + this.config.sessionTtlSeconds * 1000,
    });

    ctx.cookies.set('admin-session', sessionId, {
      path: '/api/v2/',
      sameSite: this.isDevOrLocal ? 'lax' : 'strict',
      httpOnly: true,
      signed: true,
      maxAge: this.config.sessionTtlSeconds * 1000,
      secure: !this.config.isLocal(),
    });

    this.logger.info(`Admin session created for ${sub} (${email})`);

    ctx.status = 303;
    ctx.redirect(stateCookie.redirectTo);
  }

  async getMe(ctx: Context) {
    const user = ctx.state.user;
    if (!user) {
      ctx.status = 401;
      ctx.body = { error: 'Unauthorized' };
      return;
    }
    ctx.body = {
      sub: user.sub,
      email: user.email,
      groups: user.groups,
    };
  }

  async logout(ctx: Context) {
    const sessionId = ctx.cookies.get('admin-session', { signed: true });
    let idToken: string | undefined;

    if (sessionId) {
      const session = await this.sessionStore.getSession(sessionId);
      if (session) {
        // Try to get ID token by refreshing (for id_token_hint)
        if (session.refreshToken) {
          try {
            const refreshed = await this.oidcClient.refreshAccessToken(session.refreshToken);
            idToken = refreshed.idToken;
          } catch (err) {
            this.logger.warn({ err }, 'Failed to refresh token during logout');
          }
        }
        await this.sessionStore.deleteSession(sessionId);
      }
    }

    // Clear the session cookie
    ctx.cookies.set('admin-session', '', {
      path: '/api/v2/',
      maxAge: 0,
    });

    const state = crypto.randomBytes(16).toString('hex');
    const redirectTo = (ctx.query.redirectTo as string) || '/';

    ctx.cookies.set(`auth-state-${state}`, JSON.stringify({ redirectTo }), {
      path: '/api/v2/auth/logout-callback',
      sameSite: 'lax',
      httpOnly: true,
      signed: true,
      maxAge: 600_000,
      secure: !this.config.isLocal(),
    });

    const postLogoutRedirectUri =
      new URL('/api/v2/auth/logout-callback', this.config.baseUrl).toString() + `?state=${encodeURIComponent(state)}`;

    const endSessionUrl = await this.oidcClient.getEndSessionUrl(postLogoutRedirectUri, idToken);

    ctx.status = 303;
    ctx.redirect(endSessionUrl);
  }

  async logoutCallback(ctx: Context) {
    const state = ctx.query.state as string;
    let redirectTo = '/';

    if (state) {
      const rawCookie = ctx.cookies.get(`auth-state-${state}`, { signed: true });
      if (rawCookie) {
        try {
          const parsed = JSON.parse(rawCookie);
          redirectTo = parsed.redirectTo || '/';
        } catch {
          // ignore corrupt cookie
        }
        ctx.cookies.set(`auth-state-${state}`, '', { path: '/api/v2/auth/logout-callback' });
      }
    }

    ctx.status = 303;
    ctx.redirect(redirectTo);
  }

  private isValidRedirectUrl(url: string): boolean {
    // Allow relative URLs
    if (url.startsWith('/')) {
      return true;
    }

    try {
      const parsed = new URL(url);
      const validProtocols = ['https:'];
      if (this.isDevOrLocal) {
        validProtocols.push('http:');
      }
      if (!validProtocols.includes(parsed.protocol)) {
        return false;
      }
      // In local/dev, allow localhost
      if (this.isDevOrLocal && parsed.hostname === 'localhost') {
        return true;
      }
      // In production, must match base domain
      const baseDomain = new URL(this.config.baseUrl).hostname;
      return parsed.hostname === baseDomain || parsed.hostname.endsWith(`.${baseDomain}`);
    } catch {
      return false;
    }
  }
}
