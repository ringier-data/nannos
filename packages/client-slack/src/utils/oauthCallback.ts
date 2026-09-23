import { Logger } from '../utils/logger.js';
import type { IUserAuthService } from '../services/userAuthService.js';
import type { IOAuthStateStore } from '../storage/types.js';

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Where the success page sends the user back to, e.g. `slack://open?team=T123`. */
export interface CallbackReturnLink {
  url: string;
  label: string;
}

/**
 * Handle OAuth callback
 */
export async function handleOAuthCallback(
  queryParams: URLSearchParams,
  userAuthService: IUserAuthService,
  baseUrl: string,
  oauthStateStore: IOAuthStateStore
): Promise<{ success: boolean; message: string; userId?: string; teamId?: string }> {
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

  const { userId, teamId, codeVerifier } = stateData;

  try {
    // Build the full callback URL for openid-client
    const callbackUrl = `${baseUrl}?${queryParams.toString()}`;

    // Complete OAuth flow
    logger.info(`Completing OAuth flow for user ${userId}`);
    await userAuthService.completeOAuthFlow(userId, teamId, callbackUrl, codeVerifier, state);

    logger.info(`Successfully authorized user ${userId}`);
    return {
      success: true,
      message: 'Authorization successful! You can now start to use the Slack bot.',
      userId,
      teamId,
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
 *
 * With *returnLink*, the success page links back to the chat app and opens it after a
 * moment: a tab the chat app opened cannot close itself, so the link is what returns
 * the user.
 */
export function generateCallbackHTML(success: boolean, message: string, returnLink?: CallbackReturnLink): string {
  const safeMessage = escapeHtml(message);
  if (success) {
    const back = returnLink
      ? `
  <p><a href="${escapeHtml(returnLink.url)}">${escapeHtml(returnLink.label)}</a></p>
  <script>
    setTimeout(() => { window.location.href = ${JSON.stringify(returnLink.url).replace(/</g, '\\u003c')}; }, 1500);
  </script>`
      : '';
    return `
<!DOCTYPE html>
<html>
<head>
  <title>Authorization Successful</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>${back}
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

Please try again by sending another message.

---

❌ Autorisierung fehlgeschlagen

${safeMessage}

Bitte versuchen Sie es erneut, indem Sie eine neue Nachricht senden.

---

❌ Échec de l'autorisation

${safeMessage}

Veuillez réessayer en envoyant un autre message.

---

❌ Autorizzazione fallita

${safeMessage}

Riprova inviando un altro messaggio.
  </pre>
</body>
</html>`;
  }
}
