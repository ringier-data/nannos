import { Logger } from '../utils/logger.js';
import { UserAuthService } from '../services/userAuthService.js';
import type { IOAuthStateStore } from '../storage/types.js';

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Handle OAuth callback
 */
export async function handleOAuthCallback(
  queryParams: URLSearchParams,
  userAuthService: UserAuthService,
  baseUrl: string,
  oauthStateStore: IOAuthStateStore
): Promise<{ success: boolean; message: string; userId?: string; projectId?: string }> {
  const logger = Logger.getLogger('handleOAuthCallback');

  const code = queryParams.get('code');
  const state = queryParams.get('state');
  const error = queryParams.get('error');
  const errorDescription = queryParams.get('error_description');

  // Handle OAuth error
  if (error) {
    logger.error(`OAuth error: ${error}: ${errorDescription}`);
    return {
      success: false,
      message: `Authorization failed: ${error}: ${errorDescription || 'No description provided'}`,
    };
  }

  // Validate required parameters
  if (!code || !state) {
    logger.error('Missing code or state in OAuth callback');
    return {
      success: false,
      message: 'Invalid authorization callback: missing parameters',
    };
  }

  // Validate and consume state
  const stateData = await oauthStateStore.consume(state);

  if (!stateData) {
    logger.error(`Invalid or expired OAuth state: ${state}`);
    return {
      success: false,
      message: 'Invalid or expired authorization request. Please try again.',
    };
  }

  const { userId, projectId, codeVerifier } = stateData;

  try {
    // Build the full callback URL for openid-client
    const callbackUrl = `${baseUrl}?${queryParams.toString()}`;

    // Complete OAuth flow
    logger.info(`Completing OAuth flow for user ${userId}`);
    await userAuthService.completeOAuthFlow(userId, projectId, callbackUrl, codeVerifier, state);

    logger.info(`Successfully authorized user ${userId}`);
    return {
      success: true,
      message: 'Authorization successful! You can now use A2A features.',
      userId,
      projectId,
    };
  } catch (error) {
    logger.error(error, `Failed to complete OAuth flow: ${error}`);
    return {
      success: false,
      message: 'Failed to complete authorization. Please try again.',
    };
  }
}

/**
 * Generate HTML response for OAuth callback
 */
export function generateCallbackHTML(success: boolean, message: string): string {
  const safeMessage = escapeHtml(message);
  if (success) {
    return `
<!DOCTYPE html>
<html>
<head>
  <title>Authorization Successful</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <pre>
✅ Authorization Successful!

${safeMessage}

You can close this window now. It will close automatically in 5 seconds.

---

✅ Autorisierung erfolgreich!

Autorisierung erfolgreich! Sie können dieses Fenster schliessen. Ihre Anfrage wird bearbeitet.

Sie können dieses Fenster jetzt schliessen. Es schliesst sich automatisch in 5 Sekunden.

---

✅ Autorisation réussie !

Autorisation réussie ! Vous pouvez fermer cette fenêtre. Votre demande est en cours de traitement.

Vous pouvez fermer cette fenêtre maintenant. Elle se fermera automatiquement dans 5 secondes.

---

✅ Autorizzazione riuscita!

Autorizzazione riuscita! Puoi chiudere questa finestra. La tua richiesta è in fase di elaborazione.

Puoi chiudere questa finestra ora. Si chiuderà automaticamente tra 5 secondi.
  </pre>
  <script>
    setTimeout(() => { window.close(); }, 5000);
  </script>
</body>
</html>`;
  } else {
    return `
<!DOCTYPE html>
<html>
<head>
  <title>Authorization Failed</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <pre>
❌ Authorization Failed

${safeMessage}

Please try again by sending another email.

---

❌ Autorisierung fehlgeschlagen

${safeMessage}

Bitte versuchen Sie es erneut, indem Sie eine neue E-Mail senden.

---

❌ Échec de l'autorisation

${safeMessage}

Veuillez réessayer en envoyant un autre e-mail.

---

❌ Autorizzazione fallita

${safeMessage}

Riprova inviando un'altra e-mail.
  </pre>
</body>
</html>`;
  }
}
