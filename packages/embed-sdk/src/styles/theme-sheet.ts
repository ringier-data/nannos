/**
 * `themeSheet()` — the TYPED face of the host theming contract.
 *
 * The contract itself lives in `theme.css`: every custom property declared on
 * `:host` may be overridden by a host sheet passed through the `styles` prop
 * (adopted after the SDK sheet — cascade wins). Hand-writing that sheet is
 * stringly: no autocomplete, typos silently resolve to defaults, and the SDK's
 * own `:host(.dark)` block outranks a plain `:host` override the moment a host
 * stamps `dark` (higher specificity beats sheet order). This helper closes all
 * three gaps — the interface IS the token list, and dark handling is built in.
 *
 * Semantics: `theme` is the host's brand — it applies in BOTH color schemes
 * (emitted on `:host` and re-emitted on `:host(.dark)`, so it also beats the
 * SDK's built-in dark palette). `dark` refines individual tokens for dark mode
 * only. A host that themes surfaces (background/card/…) for both schemes passes
 * both arguments; a host that only brands the accent passes one.
 *
 * Shadow mode only — `:host` matches nothing in light DOM (`shadow={false}`),
 * where host CSS already applies natively.
 */

/** Overridable design tokens. Every field maps to one custom property from the
 *  `:host` block of `theme.css`; omitted fields keep the SDK default. */
export interface NannosTheme {
  /** Brand color (`--nannos-accent`) — derives `--primary` and `--ring`, so this
   *  one knob re-brands the palette. (shadcn's own `--accent` hover token is a
   *  different thing — that one is `accentSurface`.) */
  accent?: string;
  /** Text/icon color on the brand color (`--nannos-accent-foreground`). */
  accentForeground?: string;
  /** Corner radius (`--nannos-radius`) — derives the sm/md/lg/xl scale. */
  radius?: string;
  /** Panel surface (`--background`). */
  background?: string;
  /** Default text (`--foreground`). */
  foreground?: string;
  /** Card surface (`--card`). */
  card?: string;
  /** Text on cards (`--card-foreground`). */
  cardForeground?: string;
  /** Popover/menu surface (`--popover`). */
  popover?: string;
  /** Text in popovers (`--popover-foreground`). */
  popoverForeground?: string;
  /** Primary action color (`--primary`) — normally derived from `accent`; set
   *  directly only to break that derivation. */
  primary?: string;
  /** Text on primary (`--primary-foreground`). */
  primaryForeground?: string;
  /** Secondary action surface (`--secondary`). */
  secondary?: string;
  /** Text on secondary (`--secondary-foreground`). */
  secondaryForeground?: string;
  /** Muted surface (`--muted`). */
  muted?: string;
  /** De-emphasized text (`--muted-foreground`). */
  mutedForeground?: string;
  /** Hover/highlight surface (`--accent`) — what shadcn's `bg-accent` paints
   *  (hovered menu rows, selected list items). Unrelated to `accent`, which is
   *  the brand knob. */
  accentSurface?: string;
  /** Text on the hover/highlight surface (`--accent-foreground`). */
  accentSurfaceForeground?: string;
  /** Errors and destructive actions (`--destructive`). */
  destructive?: string;
  /** Borders and separators (`--border`). */
  border?: string;
  /** Input borders (`--input`). */
  input?: string;
  /** Focus ring (`--ring`) — normally derived from `accent`. */
  ring?: string;
  /** Chart series colors (`--chart-1` … `--chart-5`). */
  chart1?: string;
  chart2?: string;
  chart3?: string;
  chart4?: string;
  chart5?: string;
  /** Escape hatch: raw custom properties emitted verbatim (tokens added after
   *  this type, or anything the interface does not name). */
  vars?: Record<`--${string}`, string>;
}

/** One entry per typed field — `Record<keyof, …>` makes the compiler refuse a
 *  token that exists in only one of interface and map. */
const TOKEN_TO_VAR: Record<Exclude<keyof NannosTheme, 'vars'>, `--${string}`> = {
  accent: '--nannos-accent',
  accentForeground: '--nannos-accent-foreground',
  radius: '--nannos-radius',
  background: '--background',
  foreground: '--foreground',
  card: '--card',
  cardForeground: '--card-foreground',
  popover: '--popover',
  popoverForeground: '--popover-foreground',
  primary: '--primary',
  primaryForeground: '--primary-foreground',
  secondary: '--secondary',
  secondaryForeground: '--secondary-foreground',
  muted: '--muted',
  mutedForeground: '--muted-foreground',
  accentSurface: '--accent',
  accentSurfaceForeground: '--accent-foreground',
  destructive: '--destructive',
  border: '--border',
  input: '--input',
  ring: '--ring',
  chart1: '--chart-1',
  chart2: '--chart-2',
  chart3: '--chart-3',
  chart4: '--chart-4',
  chart5: '--chart-5',
};

function declarationsOf(theme: NannosTheme): string[] {
  const tokens = (Object.keys(TOKEN_TO_VAR) as Array<Exclude<keyof NannosTheme, 'vars'>>)
    .filter((token) => theme[token] != null)
    .map((token) => `  ${TOKEN_TO_VAR[token]}: ${theme[token]};`);
  const raw = Object.entries(theme.vars ?? {}).map(([name, value]) => `  ${name}: ${value};`);
  return [...tokens, ...raw];
}

/**
 * Build a host override sheet for the `styles` prop of `AssistantPanel` /
 * `ShadowPortal` from typed tokens.
 *
 * ```tsx
 * <AssistantPanel styles={[themeSheet({ accent: '#bb448b', accentForeground: '#fff' })]} />
 * ```
 *
 * @param theme Brand tokens for both color schemes (they override the SDK's
 *   dark palette too — see the module comment).
 * @param dark Per-token dark-mode refinements, applied when the host stamps
 *   `dark` on the panel (`hostClassName="dark"`).
 */
export function themeSheet(theme: NannosTheme, dark?: NannosTheme): string {
  const base = declarationsOf(theme);
  const darkDecls = declarationsOf({
    ...theme,
    ...dark,
    vars: { ...theme.vars, ...dark?.vars },
  });
  const blocks: string[] = [];
  if (base.length > 0) blocks.push(`:host {\n${base.join('\n')}\n}`);
  if (darkDecls.length > 0) blocks.push(`:host(.dark) {\n${darkDecls.join('\n')}\n}`);
  return blocks.join('\n\n');
}
