/**
 * Display title for a tool part.
 *
 * `client_action` is ONE tool doing four unrelated jobs — `apply`, `highlight`,
 * `navigate`, `read_current_page` — so a card titled only `client_action` hides
 * the single thing a reader is looking for. Append the directive's kind:
 * `client_action · read_current_page`.
 *
 * Two different shapes carry that kind, because the tool reaches the UI on two
 * different paths:
 *
 *   - the awaited round trip surfaces the WIRE directive, wrapped:
 *     `{ directive: { kind, target: { type, id } }, _clientActionRequest: true }`;
 *   - the risk-gate approval (an `apply`) surfaces the agent's raw TOOL ARGS,
 *     which are flat and snake_case: `{ kind, target_type, target_id, values }`.
 *
 * Both are unvalidated wire data, so every hop is read defensively: anything
 * unreadable falls back to the bare tool name rather than a title with an
 * `undefined` in it.
 */

/** The agent-side tool name these directives arrive under. */
export const CLIENT_ACTION_TOOL = 'client_action';

/** Non-empty strings only — a blank `kind` must not produce a trailing ` · `. */
function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

/** The directive `kind` behind a `client_action` part, whichever shape it took. */
export function clientActionKind(input: unknown): string | null {
  const raw = input as { kind?: unknown; directive?: { kind?: unknown } } | null | undefined;
  return text(raw?.directive?.kind) ?? text(raw?.kind);
}

export function toolPartTitle(toolName: string, input: unknown): string {
  if (toolName !== CLIENT_ACTION_TOOL) return toolName;
  const kind = clientActionKind(input);
  return kind ? `${toolName} · ${kind}` : toolName;
}

/** i18n key describing a self-evident `client_action` kind, or null when the
 *  kind is unknown (a new/unreadable kind must fall back to the raw args, not
 *  to a confident sentence about the wrong thing). The agent no longer sends a
 *  `_summary` for this tool — the enum is closed, so the SDK says it in the
 *  user's own language instead of paying a fast-LLM call to translate. */
export function clientActionSummaryKey(
  input: unknown,
):
  | 'hitl.clientAction.apply'
  | 'hitl.clientAction.highlight'
  | 'hitl.clientAction.navigate'
  | 'hitl.clientAction.readCurrentPage'
  | null {
  switch (clientActionKind(input)) {
    case 'apply':
      return 'hitl.clientAction.apply';
    case 'highlight':
      return 'hitl.clientAction.highlight';
    case 'navigate':
      return 'hitl.clientAction.navigate';
    case 'read_current_page':
      return 'hitl.clientAction.readCurrentPage';
    default:
      return null;
  }
}
