import { describe, test, expect } from '@jest/globals';
import { generateCallbackHTML, pickCallbackLocale } from '../../src/utils/oauthCallback.js';

describe('generateCallbackHTML', () => {
  test('escapes the message', () => {
    const html = generateCallbackHTML(false, 'Authorization failed: <script>alert(1)</script>');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  test('renders in the browser language only', () => {
    const html = generateCallbackHTML(true, 'Done', { acceptLanguage: 'it-CH,it;q=0.9,en;q=0.8' });
    expect(html).toContain('<html lang="it">');
    expect(html).toContain('<title>Autorizzazione riuscita</title>');
    expect(html).toContain('la risposta arriverà via e-mail.');
    expect(html).not.toContain('Authorization successful');
    expect(html).not.toContain('Autorisierung erfolgreich');
  });

  test('asks for another email on failure', () => {
    const html = generateCallbackHTML(false, 'No', { acceptLanguage: 'de' });
    expect(html).toContain('Bitte versuchen Sie es erneut, indem Sie eine neue E-Mail senden.');
    expect(html).not.toContain('<script>');
  });

  test('shows the logo as favicon and in the card', () => {
    const html = generateCallbackHTML(true, 'Done');
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,');
    expect(html).toContain('<img src="data:image/svg+xml;base64,');
    expect(html).toContain('window.close()');
  });
});

describe('pickCallbackLocale', () => {
  test.each([
    [undefined, 'en'],
    ['fr-CH', 'fr'],
    ['en;q=0.5,de;q=0.8', 'de'],
    ['ja,zh', 'en'],
    ['constructor', 'en'],
  ])('%p -> %p', (header, locale) => {
    expect(pickCallbackLocale(header)).toBe(locale);
  });
});
