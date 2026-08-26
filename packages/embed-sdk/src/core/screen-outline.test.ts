// @vitest-environment happy-dom
/**
 * The screen-outline DOM walk (everything it emits reaches a model on demand).
 * Behaviors ported from Gatana's screen-snapshot, plus the SDK's host-agnostic
 * additions: ARIA grids (MUI X DataGrid), MUI skeletons/breadcrumbs, role=alert
 * toasts. happy-dom implements `checkVisibility`, so the hidden-element cases
 * exercise the real gate, not the assume-visible fallback.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { snapshotScreenOutline } from './screen-outline';

function setBody(html: string) {
  document.body.innerHTML = html;
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('snapshotScreenOutline', () => {
  it('turns headings and text into a markdown outline', () => {
    setBody(`<main>
      <h1>Campaigns</h1>
      <p>12 running</p>
      <div role="heading" aria-level="3">By status</div>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('# Campaigns');
    expect(out).toContain('12 running');
    expect(out).toContain('### By status');
  });

  it('prefers the marked read root over <main>', () => {
    setBody(`<main><p>chrome around the app</p>
      <section data-nannos-read-root=""><p>the actual page</p></section>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('the actual page');
    expect(out).not.toContain('chrome around the app');
  });

  it('drops ignored subtrees and redacts marked ones', () => {
    setBody(`<main>
      <div data-nannos-ignore=""><p>internal nav the model should not see</p></div>
      <p>Webhook URL: <span data-nannos-redact="">https://hooks.example/secret</span></p>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).not.toContain('internal nav');
    expect(out).not.toContain('hooks.example');
    expect(out).toContain('[redacted]');
  });

  it('skips what the user cannot see', () => {
    setBody(`<main>
      <p>visible</p>
      <p style="display:none">display-none</p>
      <p aria-hidden="true">aria-hidden</p>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('visible');
    expect(out).not.toContain('display-none');
    expect(out).not.toContain('aria-hidden');
  });

  it('renders a real <table> as a markdown table with a header separator', () => {
    setBody(`<main><table>
      <thead><tr><th>Name</th><th>Status</th></tr></thead>
      <tbody><tr><td>Summer push</td><td>running</td></tr></tbody>
    </table></main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('| Name | Status |');
    expect(out).toContain('| --- | --- |');
    expect(out).toContain('| Summer push | running |');
  });

  it('renders an ARIA grid (MUI X DataGrid shape) as a markdown table', () => {
    setBody(`<main><div role="grid">
      <div role="row">
        <div role="columnheader">Line item</div><div role="columnheader">Budget</div>
      </div>
      <div role="row">
        <div role="gridcell">Homepage banner</div><div role="gridcell">CHF 5000</div>
      </div>
    </div></main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('| Line item | Budget |');
    expect(out).toContain('| --- | --- |');
    expect(out).toContain('| Homepage banner | CHF 5000 |');
  });

  it('reads form controls as their value, never a password', () => {
    setBody(`<main>
      <input value="Autumn sale" />
      <input type="password" value="hunter2" />
      <input type="checkbox" checked />
      <div role="switch" aria-checked="true">Auto-publish</div>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('[Autumn sale]');
    expect(out).not.toContain('hunter2');
    expect(out).toContain('[hidden]');
    expect(out).toContain('[x]');
    expect(out).toContain('[on]');
  });

  it('says the page is still loading when skeletons are on screen (both kits)', () => {
    setBody(`<main>
      <div data-slot="skeleton"></div>
      <span class="MuiSkeleton-root"></span>
      <p>what already loaded</p>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('what already loaded');
    expect(out).toContain('still loading');
  });

  it('skips breadcrumbs by slot and by ARIA name', () => {
    setBody(`<main>
      <nav aria-label="breadcrumb"><a>Home</a><a>Campaigns</a></nav>
      <nav data-slot="breadcrumb"><a>Home2</a></nav>
      <p>content</p>
    </main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).not.toContain('Home');
    expect(out).toContain('content');
  });

  it('reports an open dialog rendered in a portal outside the root', () => {
    setBody(`<main><p>page behind</p></main>
      <div role="dialog"><h2>Confirm archive</h2><p>This cannot be undone.</p></div>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('## Open dialog');
    expect(out).toContain('Confirm archive');
  });

  it('reports toasts (sonner and role=alert) outside the root, once', () => {
    setBody(`<main><p role="alert">inline validation error</p></main>
      <div data-sonner-toast=""><p>Saved</p></div>
      <div role="alert">Budget update failed</div>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('## Notifications on screen');
    expect(out).toContain('- Saved');
    expect(out).toContain('- Budget update failed');
    // The inline alert was already walked as page content — not repeated as a notification.
    expect(out.split('inline validation error').length).toBe(2);
  });

  it('lists tabs and marks the open one', () => {
    setBody(`<main><div role="tablist">
      <button role="tab" aria-selected="true">Overview</button>
      <button role="tab">Line items</button>
    </div></main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('Tabs: Overview (open) | Line items');
  });

  it('labels icon-only controls by their accessible name', () => {
    setBody(`<main><button aria-label="Delete campaign"></button></main>`);
    expect(snapshotScreenOutline(5000)).toContain('[Delete campaign]');
  });

  it('cuts mid-walk at the budget and says so', () => {
    const rows = Array.from({ length: 30 }, (_, i) => `<p>row ${i} ${'x'.repeat(80)}</p>`).join('');
    setBody(`<main>${rows}</main>`);
    const out = snapshotScreenOutline(500);
    expect(out.length).toBeLessThan(700);
    expect(out).toContain('did not fit');
    expect(out).not.toContain('row 29');
  });

  it('keeps the tail of a long <pre> (a log), not the head', () => {
    const log = Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n');
    setBody(`<main><pre>${log}</pre></main>`);
    const out = snapshotScreenOutline(5000);
    expect(out).toContain('line 39');
    expect(out).not.toContain('line 0\n');
    expect(out).toContain('earlier lines');
  });

  it('returns an empty string for a page with nothing to read', () => {
    setBody('<main></main>');
    expect(snapshotScreenOutline(5000)).toBe('');
  });
});
