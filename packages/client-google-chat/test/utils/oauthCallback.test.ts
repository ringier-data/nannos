import { describe, test, expect } from '@jest/globals';
import { generateCallbackHTML, pickCallbackLocale } from '../../src/utils/oauthCallback.js';

describe('generateCallbackHTML', () => {
  test('escapes the message', () => {
    const html = generateCallbackHTML(false, 'Authorization failed: <script>alert(1)</script>');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  test('links back to the chat app on success', () => {
    const html = generateCallbackHTML(true, 'Done', { returnUrl: 'https://chat.google.com' });
    expect(html).toContain('<a class="button" href="https://chat.google.com">Back to Google Chat</a>');
    expect(html).toContain('window.location.href = "https://chat.google.com"');
  });

  test('no link without one, and never on failure', () => {
    expect(generateCallbackHTML(true, 'Done')).not.toContain('href="https://chat.google.com"');
    expect(generateCallbackHTML(false, 'No', { returnUrl: 'https://chat.google.com' })).not.toContain(
      'https://chat.google.com'
    );
  });

  test('a link cannot break out of the script', () => {
    const html = generateCallbackHTML(true, 'Done', { returnUrl: 'https://chat.google.com/?x=</script><img src=x>' });
    expect(html).not.toContain('</script><img');
  });

  test('renders in the browser language only', () => {
    const html = generateCallbackHTML(false, 'No', { acceptLanguage: 'fr-CH,fr;q=0.9,en;q=0.8' });
    expect(html).toContain('<html lang="fr">');
    expect(html).toContain("<title>Échec de l'autorisation</title>");
    expect(html).toContain('Veuillez réessayer en envoyant un autre message.');
    expect(html).not.toContain('Authorization failed</');
    expect(html).not.toContain('Autorisierung fehlgeschlagen');
  });

  test('shows the logo as favicon and in the card', () => {
    const html = generateCallbackHTML(true, 'Done');
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,');
    expect(html).toContain('<img src="data:image/svg+xml;base64,');
  });
});

describe('pickCallbackLocale', () => {
  test.each([
    [undefined, 'en'],
    ['de-CH', 'de'],
    ['es-ES,es;q=0.9,it;q=0.5', 'it'],
    ['en;q=0.5,de;q=0.8', 'de'],
    ['de;q=0,fr', 'fr'],
    ['ja,zh', 'en'],
    ['constructor', 'en'],
  ])('%p -> %p', (header, locale) => {
    expect(pickCallbackLocale(header)).toBe(locale);
  });
});
