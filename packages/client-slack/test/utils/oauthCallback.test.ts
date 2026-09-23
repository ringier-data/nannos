import { describe, test, expect } from '@jest/globals';
import { generateCallbackHTML } from '../../src/utils/oauthCallback.js';

describe('generateCallbackHTML', () => {
  test('escapes the message', () => {
    const html = generateCallbackHTML(false, 'Authorization failed: <script>alert(1)</script>');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  test('links back to the chat app on success', () => {
    const html = generateCallbackHTML(true, 'Done', { url: 'slack://open?team=T1', label: 'Back to Slack' });
    expect(html).toContain('<a href="slack://open?team=T1">Back to Slack</a>');
    expect(html).toContain('window.location.href = "slack://open?team=T1"');
  });

  test('no link without one, and never on failure', () => {
    expect(generateCallbackHTML(true, 'Done')).not.toContain('<a href');
    expect(generateCallbackHTML(false, 'No', { url: 'slack://open', label: 'Back' })).not.toContain('<a href');
  });

  test('a link cannot break out of the script', () => {
    const html = generateCallbackHTML(true, 'Done', { url: 'slack://open?x=</script><img src=x>', label: 'Back' });
    expect(html).not.toContain('</script><img');
  });
});
