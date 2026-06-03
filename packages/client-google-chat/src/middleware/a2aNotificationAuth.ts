import { Request, Response, NextFunction } from 'express';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { InstallationSecretService } from '../services/installationSecretService.js';

const logger = Logger.getLogger('A2ANotificationAuth');

/**
 * Middleware to validate A2A push notification tokens.
 *
 * Matches the X-A2A-Notification-Token header against the per-installation
 * secret stored in AWS SSM Parameter Store (one secret per Google Chat
 * bot, keyed by `botName`). On success, sets `res.locals.projectNumber`.
 */
export function createA2ANotificationAuthMiddleware(
  googleChatConfigs: Config['googleChatConfigs'],
  installationSecretService: InstallationSecretService
) {
  return async (req: Request, res: Response, next: NextFunction): Promise<void> => {
    const notificationToken = req.headers['x-a2a-notification-token'] as string | undefined;
    if (!notificationToken) {
      logger.warn('[A2ANotificationAuth] Missing X-A2A-Notification-Token header');
      res.status(401).json({ error: 'Missing notification token' });
      return;
    }

    for (const project of googleChatConfigs) {
      try {
        const secret = await installationSecretService.get(project.botName);
        if (secret && secret === notificationToken) {
          res.locals.projectNumber = project.projectNumber;
          next();
          return;
        }
      } catch (err) {
        logger.warn(`Failed to resolve secret for bot=${project.botName}: ${err}`);
      }
    }

    logger.warn('[A2ANotificationAuth] Invalid notification token — no matching project');
    res.status(403).json({ error: 'Invalid notification token' });
  };
}
