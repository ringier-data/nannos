import Koa from 'koa';
import helmet from 'koa-helmet';
import { koaBody } from 'koa-body';
import cors from '@koa/cors';
import { Config } from './config/config.js';
import { Router } from '@koa/router';
import { registerV2Routes } from './routes/v2routes.js';
import { BotInstallationsController } from './controllers/BotInstallationsController.js';
import { ConfigController } from './controllers/ConfigController.js';
import { OpenApiRouter } from './routes/openApiRouter.js';
import { openApiUiMiddleware } from './middlewares/openApiUiMiddleware.js';
import { Logger } from './utils/logger.js';
import type { StorageProvider } from './storage/StorageProvider.js';
import { OIDCClient } from './services/oidcClient.js';
import { AuthController } from './controllers/AuthController.js';
import { createAuthMiddleware } from './middlewares/authMiddleware.js';

export const getV2App = (config: Config, storage: StorageProvider, oidcClient: OIDCClient): Koa => {
  const isDevOrLocal = config.isDev() || config.isLocal();
  const isStg = config.isStg();
  const app = new Koa();
  app.keys = [config.v2CookieSecret];

  app.use(async (ctx, next) => {
    // health endpoint
    if (ctx.path === `/api/v2/health`) {
      ctx.status = 200;
      ctx.body = 'OK';
      return;
    }
    return next();
  });

  const requestLogger = Logger.getLogger('requestLogger');
  app.use(async (ctx, next) => {
    return next().finally(() => {
      requestLogger.info(`${ctx.method} ${ctx.path} - ${ctx.status}`);
    });
  });

  app.use(
    cors({
      credentials: true,
      exposeHeaders: ['X-Trace-Id'],
      origin: (ctx) => {
        const origin = ctx.request.get('origin');
        const localhost = new RegExp(`^https?://localhost(:\\d+)?$`, 'i');
        if ((isDevOrLocal && localhost.test(origin)) || isDevOrLocal || isStg) {
          return origin;
        }
        return '';
      },
    })
  );

  app.use(
    helmet({
      contentSecurityPolicy: {
        directives: { 'default-src': [`'none'`] },
      },
      crossOriginResourcePolicy: {
        policy: isDevOrLocal || isStg ? 'cross-origin' : 'same-origin',
      },
    })
  );

  app.use(createAuthMiddleware(config, storage.adminSession, oidcClient));
  app.use(
    koaBody({
      patchNode: true,
      patchKoa: true,
      multipart: false,
      includeUnparsed: true,
    })
  );

  const router = new Router();
  const openApiRouter = new OpenApiRouter(router);

  const installationsController = new BotInstallationsController(storage.botInstallation);

  // Auth routes (login/callback/logout) — registered before API routes
  const authController = new AuthController(config, storage.adminSession, oidcClient);
  router.get('/api/v2/auth/login', (ctx) => authController.getLogin(ctx));
  router.get('/api/v2/auth/callback', (ctx) => authController.getLoginCallback(ctx));
  router.get('/api/v2/auth/me', (ctx) => authController.getMe(ctx));
  router.get('/api/v2/auth/logout', (ctx) => authController.logout(ctx));
  router.get('/api/v2/auth/logout-callback', (ctx) => authController.logoutCallback(ctx));

  registerV2Routes(openApiRouter, installationsController, new ConfigController(config));

  // Avatar routes — binary/multipart, registered directly on Koa router
  const multipartParser = koaBody({ multipart: true, formidable: { maxFileSize: 2 * 1024 * 1024 } });
  router.post('/api/v2/installations/:appId/avatar', multipartParser, (ctx) =>
    installationsController.uploadAvatar(ctx)
  );
  router.get('/api/v2/installations/:appId/avatar', (ctx) => installationsController.getAvatar(ctx));
  router.delete('/api/v2/installations/:appId/avatar', (ctx) => installationsController.deleteAvatar(ctx));

  app.use(router.routes());
  app.use(openApiUiMiddleware(config, openApiRouter));

  app.proxy = true;
  return app;
};
