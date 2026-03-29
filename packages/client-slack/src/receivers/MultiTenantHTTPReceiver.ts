import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { URL } from 'node:url';
import { parse as parseQueryString } from 'node:querystring';
import { type App, type Receiver, verifySlackRequest } from '@slack/bolt';
import { HTTPResponseAck } from '@slack/bolt/dist/receivers/HTTPResponseAck.js';
import {
  bufferIncomingMessage,
  parseHTTPRequestBody,
  extractRetryNumFromHTTPRequest,
  extractRetryReasonFromHTTPRequest,
  buildNoBodyResponse,
  buildUrlVerificationResponse,
  buildContentResponse,
  defaultDispatchErrorHandler,
  defaultProcessEventErrorHandler,
  defaultUnhandledRequestHandler,
  getHeader,
} from '@slack/bolt/dist/receivers/HTTPModuleFunctions.js';
import {
  buildReceiverRoutes,
  type CustomRoute,
  type ReceiverRoutes,
} from '@slack/bolt/dist/receivers/custom-routes.js';
import type { ParamsIncomingMessage } from '@slack/bolt/dist/receivers/ParamsIncomingMessage.js';
import { match } from 'path-to-regexp';
import type { IBotInstallationStore } from '../storage/types.js';
import { Logger } from '../utils/logger.js';

/**
 * Custom HTTP receiver that verifies Slack request signatures on a per-app basis.
 *
 * Unlike Bolt's built-in HTTPReceiver which uses a single signing secret for all
 * requests, this receiver extracts `api_app_id` from the raw request body and
 * looks up the corresponding signing secret from the database before performing
 * HMAC verification.
 *
 * This enables true multi-tenant operation where each registered Slack App has
 * its own unique signing secret.
 */
export class MultiTenantHTTPReceiver implements Receiver {
  private app?: App;
  private server?: Server;
  private readonly botInstallationStore: IBotInstallationStore;
  private readonly endpoints: string[];
  private readonly port: number;
  private readonly routes: ReceiverRoutes;
  private readonly logger = Logger.getLogger(MultiTenantHTTPReceiver.name);

  constructor(options: {
    botInstallationStore: IBotInstallationStore;
    endpoints?: string | string[];
    port?: number;
    customRoutes?: CustomRoute[];
  }) {
    this.botInstallationStore = options.botInstallationStore;
    const endpoints = options.endpoints ?? ['/slack/events'];
    this.endpoints = Array.isArray(endpoints) ? endpoints : [endpoints];
    this.port = options.port ?? 3000;
    this.routes = buildReceiverRoutes(options.customRoutes ?? []);
  }

  init(app: App): void {
    this.app = app;
  }

  start(port?: number): Promise<Server> {
    const listenPort = port ?? this.port;
    if (this.server !== undefined) {
      return Promise.reject(new Error('The receiver cannot be started because it was already started.'));
    }
    this.server = createServer((req, res) => {
      this.requestListener(req, res);
    });
    return new Promise((resolve, reject) => {
      this.server!.on('error', (error) => {
        this.server?.close();
        reject(error);
      });
      this.server!.on('close', () => {
        this.server = undefined;
      });
      this.server!.listen(listenPort, () => {
        resolve(this.server!);
      });
    });
  }

  stop(): Promise<void> {
    if (this.server === undefined) {
      return Promise.reject(new Error('The receiver cannot be stopped because it was not started.'));
    }
    return new Promise((resolve, reject) => {
      this.server?.close((error) => {
        if (error !== undefined) {
          return reject(error);
        }
        this.server = undefined;
        return resolve();
      });
    });
  }

  private requestListener(req: IncomingMessage, res: ServerResponse): void {
    const { pathname: path } = new URL(req.url ?? '', 'http://localhost');
    const method = (req.method ?? 'GET').toUpperCase();

    // Slack events endpoint
    if (this.endpoints.includes(path) && method === 'POST') {
      this.handleIncomingEvent(req, res);
      return;
    }

    // Custom routes (health, OAuth, webhooks, etc.)
    const routePaths = Object.keys(this.routes);
    for (const route of routePaths) {
      const matchRegex = match(route, { decode: decodeURIComponent });
      const pathMatch = matchRegex(path);
      if (pathMatch && this.routes[route][method] !== undefined) {
        const message = Object.assign(req, { params: pathMatch.params }) as ParamsIncomingMessage;
        this.routes[route][method](message, res);
        return;
      }
    }

    // Not found
    defaultDispatchErrorHandler({
      error: new Error(`Unhandled HTTP request (${method}) made to ${path}`),
      logger: this.logger,
      request: req,
      response: res,
    });
  }

  private handleIncomingEvent(req: IncomingMessage, res: ServerResponse): void {
    (async () => {
      // Step 1: Buffer the raw body
      let bufferedReq;
      try {
        bufferedReq = await bufferIncomingMessage(req);
      } catch (err) {
        this.logger.warn(`Failed to buffer request body: ${err}`);
        buildNoBodyResponse(res, 400);
        return;
      }

      const rawBodyStr = bufferedReq.rawBody.toString();
      const contentType = req.headers['content-type'];

      // Step 2: Handle SSL check (no signature verification needed)
      if (contentType === 'application/x-www-form-urlencoded') {
        const parsedQs = parseQueryString(rawBodyStr);
        if (parsedQs?.ssl_check) {
          buildNoBodyResponse(res, 200);
          return;
        }
      }

      // Step 3: Handle URL verification challenge (no api_app_id present)
      // Parse body tentatively to check for url_verification
      let tentativeBody: any;
      try {
        if (contentType === 'application/x-www-form-urlencoded') {
          const parsedQs = parseQueryString(rawBodyStr);
          tentativeBody = typeof parsedQs.payload === 'string' ? JSON.parse(parsedQs.payload) : parsedQs;
        } else {
          tentativeBody = JSON.parse(rawBodyStr);
        }
      } catch {
        this.logger.warn('Failed to tentatively parse request body');
        buildNoBodyResponse(res, 400);
        return;
      }

      if (tentativeBody.type === 'url_verification') {
        // URL verification challenges are one-time setup events from Slack.
        // They don't contain api_app_id so we can't do per-app verification.
        // Respond with the challenge directly.
        buildUrlVerificationResponse(res, tentativeBody);
        return;
      }

      // Step 4: Extract api_app_id and look up signing secret
      const appId = tentativeBody.api_app_id as string | undefined;
      if (!appId) {
        this.logger.warn('Request missing api_app_id — cannot verify signature');
        buildNoBodyResponse(res, 401);
        return;
      }

      const bot = await this.botInstallationStore.getByAppId(appId);
      if (!bot) {
        this.logger.warn(`No active bot installation found for appId=${appId}`);
        buildNoBodyResponse(res, 401);
        return;
      }

      // Step 5: Verify signature with the per-app signing secret
      try {
        const signature = getHeader(req, 'x-slack-signature');
        const requestTimestampSec = Number(getHeader(req, 'x-slack-request-timestamp'));
        verifySlackRequest({
          signingSecret: bot.signingSecret,
          body: rawBodyStr,
          headers: {
            'x-slack-signature': signature,
            'x-slack-request-timestamp': requestTimestampSec,
          },
        });
      } catch (err) {
        this.logger.warn(`Signature verification failed for appId=${appId}: ${err}`);
        buildNoBodyResponse(res, 401);
        return;
      }

      // Step 6: Parse body and dispatch event to Bolt
      let body;
      try {
        body = parseHTTPRequestBody(bufferedReq);
      } catch (err) {
        this.logger.warn(`Malformed request body: ${err}`);
        buildNoBodyResponse(res, 400);
        return;
      }

      const ack = new HTTPResponseAck({
        logger: this.logger,
        processBeforeResponse: false,
        unhandledRequestHandler: defaultUnhandledRequestHandler,
        httpRequest: bufferedReq,
        httpRequestBody: body,
        httpResponse: res,
      });

      const event = {
        body,
        ack: ack.bind(),
        retryNum: extractRetryNumFromHTTPRequest(req),
        retryReason: extractRetryReasonFromHTTPRequest(req),
      };

      try {
        await this.app?.processEvent(event);
        if (ack.storedResponse !== undefined) {
          buildContentResponse(res, ack.storedResponse);
        }
      } catch (error) {
        const acknowledgedByHandler = await defaultProcessEventErrorHandler({
          error: error as Error,
          logger: this.logger,
          request: req,
          response: res,
          storedResponse: ack.storedResponse,
        });
        if (acknowledgedByHandler) {
          ack.ack();
        }
      }
    })();
  }
}
