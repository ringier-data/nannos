import { Request, Response, NextFunction } from 'express';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';

const logger = Logger.getLogger('A2ANotificationAuth');

/**
 * Middleware to validate A2A push notification tokens.
 *
 * Matches the X-A2A-Notification-Token header against per-project secrets
 * configured via A2A_NOTIFICATION_SECRET_<PROJECT_NAME> env vars.
 * On success, sets res.locals.projectNumber
 */
export function createA2ANotificationAuthMiddleware(googleChatConfigs: Config['googleChatConfigs']) {
  return (req: Request, res: Response, next: NextFunction): void => {
    const notificationToken = req.headers['x-a2a-notification-token'] as string | undefined;
    if (!notificationToken) {
      logger.warn('[A2ANotificationAuth] Missing X-A2A-Notification-Token header');
      res.status(401).json({ error: 'Missing notification token' });
      return;
    }

    // Resolve project by matching the secret
    for (const project of googleChatConfigs) {
      if (project.a2aNotificationSecret && project.a2aNotificationSecret === notificationToken) {
        res.locals.projectNumber = project.projectNumber;
        next();
        return;
      }
    }

    logger.warn('[A2ANotificationAuth] Invalid notification token — no matching project');
    res.status(403).json({ error: 'Invalid notification token' });
  };
}
