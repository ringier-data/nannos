import { Request, Response, NextFunction } from 'express';
import { OAuth2Client } from 'google-auth-library';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';

const logger = Logger.getLogger('GoogleChatAuth');

/**
 * Middleware to verify that incoming HTTP requests are from Google Chat.
 */
export function createGoogleChatAuthMiddleware(
  googleChatTokenExpectedAudience: string,
  googleChatConfigs: Config['googleChatConfigs'],
) {
  const oAuth2Client = new OAuth2Client();

  return async (req: Request, res: Response, next: NextFunction): Promise<void> => {
    try {
      const authHeader = req.headers.authorization;
      if (!authHeader || !authHeader.startsWith('Bearer ')) {
        logger.warn('Missing or invalid Authorization header');
        res.status(401).json({ error: 'Missing or invalid Authorization header' });
        return;
      }

      const token = authHeader.substring(7); // Remove 'Bearer '

      // verifyIdToken fetches certs from /oauth2/v1/certs and handles key rotation.
      const ticket = await oAuth2Client.verifyIdToken({
        idToken: token,
        audience: googleChatTokenExpectedAudience,
      });

      const payload = ticket.getPayload();
      if (!payload) {
        logger.warn('Token verification failed: no payload');
        res.status(401).json({ error: 'Invalid token' });
        return;
      }

      let requestedProjectNumber;
      for (const config of googleChatConfigs) {
        const expectedEmail = `service-${config.projectNumber}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`;

        if (payload.email === expectedEmail) {
          requestedProjectNumber = config.projectNumber;
          break;
        }
      }

      if (!requestedProjectNumber) {
        logger.warn(`Token issuer mismatch: expected one of the configured service accounts, got ${payload.email}`);
        res.status(401).json({ error: 'Invalid token issuer' });
        return;
      }

      res.locals.projectNumber = requestedProjectNumber;

      logger.debug('Google Chat request verified successfully');
      next();
    } catch (error) {
      logger.error(`Google Chat auth verification failed: ${error}`);
      res.status(401).json({ error: 'Authentication failed' });
    }
  };
}
