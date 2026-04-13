import { Context, Next } from 'koa';
import { decodeJwt } from 'jose';
import { Config } from '../config/config.js';
import { OIDCClient } from '../services/oidcClient.js';
import { Logger } from '../utils/logger.js';
import type { IAdminSessionStore } from '../storage/types.js';

const logger = Logger.getLogger('authMiddleware');

const PUBLIC_PATHS = new Set([
  '/api/v2/health',
  '/api/v2/openapi.json',
  '/api/v2/auth/login',
  '/api/v2/auth/callback',
  '/api/v2/auth/logout-callback',
]);

// 5-minute buffer before actual expiry to trigger refresh
const TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000;

export function createAuthMiddleware(config: Config, sessionStore: IAdminSessionStore, oidcClient: OIDCClient) {
  return async function authMiddleware(ctx: Context, next: Next) {
    if (PUBLIC_PATHS.has(ctx.path)) {
      return next();
    }

    const sessionId = ctx.cookies.get('admin-session', { signed: true });
    if (!sessionId) {
      ctx.status = 401;
      ctx.body = { error: 'Unauthorized' };
      return;
    }

    const session = await sessionStore.getSession(sessionId);
    if (!session) {
      ctx.cookies.set('admin-session', '', { path: '/api/v2/', maxAge: 0 });
      ctx.status = 401;
      ctx.body = { error: 'Session expired' };
      return;
    }

    // Auto-refresh access token if near expiry
    let { accessToken } = session;
    if (session.accessTokenExpiresAt - Date.now() < TOKEN_REFRESH_BUFFER_MS && session.refreshToken) {
      try {
        const refreshed = await oidcClient.refreshAccessToken(session.refreshToken);
        accessToken = refreshed.accessToken;
        await sessionStore.updateSession(sessionId, {
          accessToken: refreshed.accessToken,
          refreshToken: refreshed.refreshToken,
          accessTokenExpiresAt: refreshed.expiresAt,
        });
        logger.debug(`Refreshed access token for session ${sessionId}`);
      } catch (err) {
        logger.warn({ err }, `Token refresh failed for session ${sessionId}, clearing session`);
        await sessionStore.deleteSession(sessionId);
        ctx.cookies.set('admin-session', '', { path: '/api/v2/', maxAge: 0 });
        ctx.status = 401;
        ctx.body = { error: 'Session expired, please log in again' };
        return;
      }
    }

    // Verify group membership from the (potentially refreshed) access token
    const payload = decodeJwt(accessToken);
    const groups: string[] = (payload.groups as string[]) ?? [];

    if (!groups.includes(config.adminGroup)) {
      logger.warn(`User ${payload.sub} access denied: not in group ${config.adminGroup}`);
      ctx.status = 403;
      ctx.body = { error: 'Forbidden' };
      return;
    }

    ctx.state.user = {
      sub: payload.sub,
      email: payload.email as string | undefined,
      groups,
    };

    return next();
  };
}
