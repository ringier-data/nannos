import { describe, test, expect } from '@jest/globals';
import Router from '@koa/router';
import z from 'zod';
import { OpenApiRouter, OpenApiValidationError } from '../../src/routes/openApiRouter.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const updateSchema = z.object({
  teamId: z.string().optional(),
  botToken: z.string().optional(),
  signingSecret: z.string().optional(),
  botName: z.string().optional(),
  slashCommand: z.string().optional(),
});

/**
 * Register a PUT route and hand back the validation middleware the router inserted
 * ahead of the handler, so a body can be pushed through it directly.
 */
function validationMiddlewareFor(schema: z.ZodType) {
  const koaRouter = new Router();
  const captured: unknown[] = [];
  const openApiRouter = new OpenApiRouter(koaRouter);
  openApiRouter.put('/api/v2/things/:id', { requestType: schema, responseType: z.any() }, async () => {
    captured.push('handler ran');
  });
  const layer = koaRouter.stack.find((l) => l.path === '/api/v2/things/:id')!;
  // [...middleware, validate, handler] — the router splices validation in as the penultimate.
  const validate = layer.stack[layer.stack.length - 2]!;
  return {
    // koa-compose invokes middleware inside a try/catch that turns a synchronous throw into a
    // rejected promise; mirror that so these assertions exercise what Koa actually sees.
    run: async (body: unknown) =>
      (validate as (ctx: unknown, next: () => Promise<void>) => Promise<void>)(
        { request: { body }, params: { id: 'A1' }, query: {} },
        async () => {
          captured.push('next called');
        }
      ),
    captured,
  };
}

const secretBody = {
  appId: 'A11111111BB',
  teamId: 'T111111BB',
  botToken: 'xoxb-1111-2222-SUPERSECRETVALUE',
  signingSecret: 'a5080d5f1028063f65779fb20f03ca83',
  botName: 'RenamedBot',
  slashCommand: '/nannos',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('request validation', () => {
  test('rejects a body carrying a property the update schema does not declare', async () => {
    const { run } = validationMiddlewareFor(updateSchema);
    // appId belongs in the path, not the body; the admin UI used to send it anyway.
    await expect(run(secretBody)).rejects.toBeInstanceOf(OpenApiValidationError);
  });

  test('names the offending field so the caller can act on the error', async () => {
    const { run } = validationMiddlewareFor(updateSchema);
    const err = (await run(secretBody).catch((e) => e)) as OpenApiValidationError;

    expect(err.details).toEqual([{ path: '/appId', message: expect.stringContaining('additional properties') }]);
  });

  test('never carries submitted values — request bodies hold bot tokens and signing secrets', async () => {
    const { run } = validationMiddlewareFor(updateSchema);
    const err = (await run(secretBody).catch((e) => e)) as OpenApiValidationError;

    // Anything that formats the error (Koa's onerror does, with %j) writes this to the logs.
    const serialised = `${err.message} ${JSON.stringify(err.details)} ${JSON.stringify(err)}`;
    expect(serialised).not.toContain(secretBody.botToken);
    expect(serialised).not.toContain(secretBody.signingSecret);
    expect(serialised).not.toContain('xoxb-');
  });

  test('is a real Error carrying a 400, so Koa does not serve it as an opaque 500', async () => {
    const { run } = validationMiddlewareFor(updateSchema);
    const err = (await run(secretBody).catch((e) => e)) as OpenApiValidationError;

    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(400);
    expect(err.expose).toBe(true);
  });

  test('reports a nested rejected key with the path that contains it', async () => {
    const nested = z.object({ config: z.object({ known: z.string().optional() }).optional() });
    const { run } = validationMiddlewareFor(nested);
    const err = (await run({ config: { known: 'a', extra: 'b' } }).catch((e) => e)) as OpenApiValidationError;

    expect(err.details[0]!.path).toBe('/config/extra');
  });

  test('passes a body that declares only known properties', async () => {
    const { run, captured } = validationMiddlewareFor(updateSchema);
    const { appId: _appId, ...valid } = secretBody;

    await expect(run(valid)).resolves.toBeUndefined();
    expect(captured).toContain('next called');
  });
});
