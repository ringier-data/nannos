import { describe, test, expect } from '@jest/globals';
import { generateCallbackHTML } from '../../src/utils/oauthCallback.js';

describe('generateCallbackHTML', () => {
  test('escapes the message', () => {
    const html = generateCallbackHTML(false, 'Authorization failed: <script>alert(1)</script>');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  test('links back to the chat app on success', () => {
    const html = generateCallbackHTML(true, 'Done', { url: 'https://chat.google.com', label: 'Back to Google Chat' });
    expect(html).toContain('<a href="https://chat.google.com">Back to Google Chat</a>');
    expect(html).toContain('window.location.href = "https://chat.google.com"');
  });

  test('no link without one, and never on failure', () => {
    expect(generateCallbackHTML(true, 'Done')).not.toContain('<a href');
    expect(generateCallbackHTML(false, 'No', { url: 'https://chat.google.com', label: 'Back' })).not.toContain('<a href');
  });

  test('a link cannot break out of the script', () => {
    const html = generateCallbackHTML(true, 'Done', { url: 'https://chat.google.com/?x=</script><img src=x>', label: 'Back' });
    expect(html).not.toContain('</script><img');
  });
});
