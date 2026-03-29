import { OpenApiRouter } from '../routes/openApiRouter.js';
import { createDocument } from 'zod-openapi';
import { Config } from '../config/config.js';
import { Context } from 'koa';

export function openApiUiMiddleware(config: Config, ...openApiRouters: OpenApiRouter[]) {
  // Merge path specs from all routers
  const allPathSpecs = openApiRouters.reduce((acc, router) => {
    return Object.assign(acc, router.pathSpecs);
  }, {});

  const document = JSON.stringify(
    createDocument({
      openapi: '3.1.1',
      info: {
        title: 'Nannos Slack A2A v2 API',
        version: config.version,
        description: 'API for Alloy Cockpit',
      },
      servers: [
        {
          url: `/api/v2`,
        },
      ],
      paths: allPathSpecs,
    })
  );

  return async (ctx: Context) => {
    if (ctx.path === `/api/v2/openapi.json`) {
      ctx.body = document;
      ctx.type = 'application/json';
    }
  };
}
