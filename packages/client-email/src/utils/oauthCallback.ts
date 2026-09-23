import { Logger } from '../utils/logger.js';
import type { IUserAuthService } from '../services/userAuthService.js';
import { Storage } from '../storage/storage.js';
import { NANNOS_LOGO_SVG } from './nannosLogo.js';

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
  userAuthService: IUserAuthService,
  baseUrl: string,
  storage: Storage
): Promise<{ success: boolean; message: string; email?: string }> {
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
  const stateData = await storage.consumeOAuthState(state);

  if (!stateData) {
    logger.error(`Invalid or expired OAuth state: ${state}`);
    return {
      success: false,
      message: 'Invalid or expired authorization request. Please try again.',
    };
  }

  const { email, codeVerifier } = stateData;

  try {
    // Build the full callback URL for openid-client
    const callbackUrl = `${baseUrl}?${queryParams.toString()}`;

    // Complete OAuth flow
    logger.info(`Completing OAuth flow for user ${email}`);
    await userAuthService.completeOAuthFlow(email, callbackUrl, codeVerifier, state);

    logger.info(`Successfully authorized user ${email}`);
    return {
      success: true,
      message: 'Authorization successful! You can close this window. Your request is being processed.',
      email,
    };
  } catch (error) {
    logger.error(error, `Failed to complete OAuth flow: ${error}`);
    return {
      success: false,
      message: 'Failed to complete authorization. Please try again.',
    };
  }
}

const CALLBACK_TEXT = {
  en: {
    successTitle: 'Authorization successful',
    successBody: 'If you sent a request, the answer comes by email.',
    closeHint: 'You can close this window.',
    failureTitle: 'Authorization failed',
    failureBody: 'Please try again by sending another email.',
  },
  de: {
    successTitle: 'Autorisierung erfolgreich',
    successBody: 'Wenn Sie eine Anfrage gesendet haben, kommt die Antwort per E-Mail.',
    closeHint: 'Sie können dieses Fenster schliessen.',
    failureTitle: 'Autorisierung fehlgeschlagen',
    failureBody: 'Bitte versuchen Sie es erneut, indem Sie eine neue E-Mail senden.',
  },
  fr: {
    successTitle: 'Autorisation réussie',
    successBody: 'Si vous avez envoyé une demande, la réponse arrivera par e-mail.',
    closeHint: 'Vous pouvez fermer cette fenêtre.',
    failureTitle: "Échec de l'autorisation",
    failureBody: 'Veuillez réessayer en envoyant un autre e-mail.',
  },
  it: {
    successTitle: 'Autorizzazione riuscita',
    successBody: 'Se hai inviato una richiesta, la risposta arriverà via e-mail.',
    closeHint: 'Puoi chiudere questa finestra.',
    failureTitle: 'Autorizzazione fallita',
    failureBody: "Riprova inviando un'altra e-mail.",
  },
};

type CallbackLocale = keyof typeof CALLBACK_TEXT;

/** The best supported language in an `Accept-Language` header such as `de-CH,de;q=0.9,en;q=0.8`; English if none. */
export function pickCallbackLocale(acceptLanguage: string | undefined): CallbackLocale {
  const ranked = (acceptLanguage ?? '')
    .split(',')
    .map((part) => {
      const [tag, ...params] = part.trim().toLowerCase().split(';');
      const q = params.map((p) => p.trim()).find((p) => p.startsWith('q='));
      return { lang: tag.split('-')[0], q: q ? Number(q.slice(2)) : 1 };
    })
    .filter(({ q }) => q > 0)
    .sort((a, b) => b.q - a.q);
  const match = ranked.find(({ lang }) => Object.hasOwn(CALLBACK_TEXT, lang));
  return (match?.lang as CallbackLocale | undefined) ?? 'en';
}

const LOGO_DATA_URI = `data:image/svg+xml;base64,${Buffer.from(NANNOS_LOGO_SVG).toString('base64')}`;

const CHECK_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const CROSS_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';

const CALLBACK_STYLE = `
    :root {
      color-scheme: light dark;
      --bg: #f3f5f8;
      --card: #ffffff;
      --text: #16181d;
      --muted: #5d6470;
      --border: #e2e5ea;
      --ok: #16a34a;
      --fail: #dc2626;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1115;
        --card: #1a1d23;
        --text: #eceef2;
        --muted: #9aa1ad;
        --border: #2b2f37;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px 16px;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .card {
      width: 100%;
      max-width: 420px;
      padding: 40px 32px 32px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: 0 12px 32px rgba(15, 17, 21, 0.08);
      text-align: center;
    }
    .mark { position: relative; width: 64px; height: 64px; margin: 0 auto 24px; }
    .mark img { width: 64px; height: 64px; object-fit: contain; }
    .badge {
      position: absolute;
      right: -8px;
      bottom: -6px;
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border: 3px solid var(--card);
      border-radius: 50%;
      color: #ffffff;
    }
    .badge.ok { background: var(--ok); }
    .badge.fail { background: var(--fail); }
    .badge svg { width: 14px; height: 14px; }
    h1 { margin: 0 0 8px; font-size: 22px; line-height: 1.3; font-weight: 650; }
    p { margin: 0; }
    .hint { margin-top: 4px; color: var(--muted); font-size: 14px; }
    .detail {
      margin-top: 20px;
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--bg);
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }`;

/** How the callback page is rendered. */
export interface CallbackPageOptions {
  /** The browser's `Accept-Language` header, which picks the page language. */
  acceptLanguage?: string;
}

/**
 * Generate HTML response for OAuth callback
 *
 * *message* is the technical detail, shown (untranslated) on failure only.
 */
export function generateCallbackHTML(success: boolean, message: string, options: CallbackPageOptions = {}): string {
  const locale = pickCallbackLocale(options.acceptLanguage);
  const text = CALLBACK_TEXT[locale];
  const title = success ? text.successTitle : text.failureTitle;

  const content = success
    ? `<p>${text.successBody}</p>
    <p class="hint">${text.closeHint}</p>`
    : `<p>${text.failureBody}</p>
    <p class="detail">${escapeHtml(message)}</p>`;
  const script = success
    ? `
  <script>
    setTimeout(() => { window.close(); }, 5000);
  </script>`
    : '';

  return `<!DOCTYPE html>
<html lang="${locale}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title}</title>
  <link rel="icon" type="image/svg+xml" href="${LOGO_DATA_URI}">
  <style>${CALLBACK_STYLE}
  </style>
</head>
<body>
  <main class="card">
    <div class="mark">
      <img src="${LOGO_DATA_URI}" alt="Nannos">
      <span class="badge ${success ? 'ok' : 'fail'}">${success ? CHECK_ICON : CROSS_ICON}</span>
    </div>
    <h1>${title}</h1>
    ${content}
  </main>${script}
</body>
</html>`;
}
