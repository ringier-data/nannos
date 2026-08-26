/**
 * Pins the themeSheet() contract: token→variable mapping, the "base theme
 * applies in both schemes" dark mirroring (what protects a host's brand from
 * the SDK's own `:host(.dark)` palette), and the vars escape hatch.
 */
import { describe, expect, it } from 'vitest';
import { themeSheet } from './theme-sheet';

describe('themeSheet', () => {
  it('maps brand knobs to --nannos-* variables', () => {
    const css = themeSheet({ accent: '#bb448b', accentForeground: '#fff', radius: '4px' });
    expect(css).toContain('--nannos-accent: #bb448b;');
    expect(css).toContain('--nannos-accent-foreground: #fff;');
    expect(css).toContain('--nannos-radius: 4px;');
  });

  it('mirrors the base theme into :host(.dark) so it outranks the SDK dark palette', () => {
    const css = themeSheet({ accent: '#bb448b' });
    const [host, dark] = css.split('\n\n');
    expect(host).toMatch(/^:host \{\n {2}--nannos-accent: #bb448b;\n\}$/);
    expect(dark).toMatch(/^:host\(\.dark\) \{\n {2}--nannos-accent: #bb448b;\n\}$/);
  });

  it('lets the dark argument refine individual tokens', () => {
    const css = themeSheet(
      { accent: '#bb448b', background: '#fff' },
      { background: '#111' },
    );
    const [host, dark] = css.split('\n\n');
    expect(host).toContain('--background: #fff;');
    // accent carries over, background is refined
    expect(dark).toContain('--nannos-accent: #bb448b;');
    expect(dark).toContain('--background: #111;');
    expect(dark).not.toContain('#fff');
  });

  it('maps shadcn tokens to kebab-case variables', () => {
    const css = themeSheet({ cardForeground: 'red', mutedForeground: 'blue', chart1: 'green' });
    expect(css).toContain('--card-foreground: red;');
    expect(css).toContain('--muted-foreground: blue;');
    expect(css).toContain('--chart-1: green;');
  });

  it('maps the hover-surface pair to shadcn --accent, not the brand knob', () => {
    const css = themeSheet({ accent: '#bb448b', accentSurface: '#f1f5f9', accentSurfaceForeground: '#0f172a' });
    const [host] = css.split('\n\n');
    expect(host).toContain('--accent: #f1f5f9;');
    expect(host).toContain('--accent-foreground: #0f172a;');
    expect(host).toContain('--nannos-accent: #bb448b;');
  });

  it('passes raw custom properties through vars', () => {
    const css = themeSheet({ vars: { '--custom-token': 'hotpink' } }, { vars: { '--custom-token': 'navy' } });
    const [host, dark] = css.split('\n\n');
    expect(host).toContain('--custom-token: hotpink;');
    expect(dark).toContain('--custom-token: navy;');
  });

  it('returns an empty string for an empty theme', () => {
    expect(themeSheet({})).toBe('');
  });
});
