import { BotInstallationsController } from '../controllers/BotInstallationsController.js';
import { ConfigController } from '../controllers/ConfigController.js';
import { OpenApiRouter } from './openApiRouter.js';
import z from 'zod';

const installationSchema = z.object({
  appId: z.string(),
  teamId: z.string(),
  botToken: z.string(),
  signingSecret: z.string(),
  botName: z.string(),
  avatarUrl: z.string().optional(),
  slashCommand: z.string(),
});

const updateInstallationSchema = z.object({
  teamId: z.string().optional(),
  botToken: z.string().optional(),
  signingSecret: z.string().optional(),
  botName: z.string().optional(),
  avatarUrl: z.string().optional(),
  slashCommand: z.string().optional(),
});

const manifestRequestSchema = z.object({
  botName: z.string(),
  slashCommand: z.string(),
  botDescription: z.string().optional(),
  socketMode: z.boolean().optional(),
});

export function registerV2Routes(
  router: OpenApiRouter,
  controller: BotInstallationsController,
  configController: ConfigController
) {
  router.get('/api/v2/installations', { responseType: z.any() }, (ctx) => controller.getAll(ctx));

  router.get('/api/v2/installations/:appId', { responseType: z.any() }, (ctx) => controller.getByAppId(ctx));

  router.post('/api/v2/installations', { requestType: installationSchema, responseType: z.any() }, (ctx) =>
    controller.create(ctx)
  );

  router.put('/api/v2/installations/:appId', { requestType: updateInstallationSchema, responseType: z.any() }, (ctx) =>
    controller.update(ctx)
  );

  router.delete('/api/v2/installations/:appId', { responseType: z.any() }, (ctx) => controller.delete(ctx));

  // -- Config & manifest endpoints --
  router.get('/api/v2/config', { responseType: z.any() }, (ctx) => configController.getConfig(ctx));

  router.post('/api/v2/installations/manifest', { requestType: manifestRequestSchema, responseType: z.any() }, (ctx) =>
    configController.generateManifest(ctx)
  );

  return router;
}
