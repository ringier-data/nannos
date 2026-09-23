import { describe, test, expect } from '@jest/globals';
import { generateCallbackHTML, pickCallbackLocale } from '../../src/utils/oauthCallback.js';

describe('generateCallbackHTML', () => {
  test('escapes the message', () => {
    const html = generateCallbackHTML(false, 'Authorization failed: <script>alert(1)</script>');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  test('links back to the chat app on success', () => {
    const html = generateCallbackHTML(true, 'Done', { returnUrl: 'slack://open?team=T1' });
    expect(html).toContain('<a class="button" href="slack://open?team=T1">Back to Slack</a>');
    expect(html).toContain('window.location.href = "slack://open?team=T1"');
  });

  test('no link without one, and never on failure', () => {
    expect(generateCallbackHTML(true, 'Done')).not.toContain('href="slack://');
    expect(generateCallbackHTML(false, 'No', { returnUrl: 'slack://open' })).not.toContain('slack://');
  });

  test('a link cannot break out of the script', () => {
    const html = generateCallbackHTML(true, 'Done', { returnUrl: 'slack://open?x=</script><img src=x>' });
    expect(html).not.toContain('</script><img');
  });

  test('renders in the browser language only', () => {
    const html = generateCallbackHTML(true, 'Done', {
      acceptLanguage: 'de-CH,de;q=0.9,en;q=0.8',
      returnUrl: 'slack://open',
    });
    expect(html).toContain('<html lang="de">');
    expect(html).toContain('<title>Autorisierung erfolgreich</title>');
    expect(html).toContain('>Zurück zu Slack</a>');
    expect(html).not.toContain('Authorization successful');
    expect(html).not.toContain('Autorisation réussie');
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
    ['', 'en'],
    ['fr-CH', 'fr'],
    ['it', 'it'],
    ['es-ES,es;q=0.9,it;q=0.5', 'it'],
    ['en;q=0.5,de;q=0.8', 'de'],
    ['de;q=0,fr', 'fr'],
    ['ja,zh', 'en'],
    ['constructor', 'en'],
  ])('%p -> %p', (header, locale) => {
    expect(pickCallbackLocale(header)).toBe(locale);
  });
});
