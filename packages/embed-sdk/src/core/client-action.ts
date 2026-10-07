import type { ObjectRegistry } from './registry';
import type { AppliedChange, ChangeStore } from './change-store';
import type { ApplyResult, ActionOutcome } from './types';
import { clientActionDirective } from './schemas';
import { SAVE_ACTION } from './zod-form';
import { sanitizeReadResult, sanitizeReadResultWithScreen } from './page-read';
import { CLIENT_ACTION_EXT } from './extensions';

export interface ClientActionDeps {
  registry: ObjectRegistry;
  /** Host-provided navigation (e.g. react-router). Return a reason instead of navigating
   *  to refuse a route the user cannot open right now (the agent is told why, and the
   *  user stays where they are rather than being bounced by the host's route guard). */
  navigate?: (to: string) => void | string;
  /** Host-provided highlight hook (scroll-into-view / outline a field). */
  highlight?: (target: { type: string; id: string }, field?: string) => void;
  /** Notified after an `apply` that rejected AT LEAST ONE field (a clean apply
   *  doesn't call it — the filled form is its own feedback), with which fields
   *  landed vs. were rejected, so the host can surface "couldn't apply X".
   *  Absent → rejections are console.warn'd (never silent).
   *
   *  This is the ONLY place a rejection becomes visible: there is no ack channel
   *  back to the agent, so it reports the apply as done whatever happened here.
   *  A host that wires nothing leaves the user with a part-filled form and no
   *  indication of it. */
  onApplyResult?: (target: { type: string; id: string }, result: ApplyResult) => void;
  /** Where applied fields are recorded with their previous values (undo, review). */
  changes?: ChangeStore;
  /** The user approved this directive with a click. An action marked `requiresApproval`
   *  runs only then — a second line behind the agent's card, for an agent runtime that
   *  does not know the flag and would send it straight through. */
  approved?: boolean;
  /** Read what the user sees in these fields BEFORE an `apply` writes them (the
   *  display text, e.g. a select's label rather than its code). Opaque to the core;
   *  handed back to `markChanged`. */
  beforeApply?: (target: { type: string; id: string }, fields: string[]) => unknown;
  /** Mark the fields an `apply` wrote, so the user sees what changed (nothing is
   *  saved yet). Each change can undo itself; `captured` is what `beforeApply` read. */
  markChanged?: (target: { type: string; id: string }, changes: AppliedChange[], captured: unknown) => void;
  /** Drop the marks of a target — after an action that saves (its `save`) succeeded. */
  clearChanged?: (target: { type: string; id: string }) => void;
  /** Answers `read_current_page`: the raw "what does the user see" object (the
   *  provider assembles it from the merged page context + registered readers).
   *  Sanitized HERE (`sanitizeReadResult`) before anything leaves the browser. */
  readCurrentPage?: () => unknown | Promise<unknown>;
  /** The rendered page as a markdown outline within a budget (the DOM walk,
   *  `snapshotScreenOutline`). When present, every read carries it under the
   *  reserved `screen` key, sized to the budget the readers left. */
  screenOutline?: (maxChars: number) => string;
}

export type ClientActionResult =
  | {
      ok: true;
      applied?: string[];
      rejected?: ApplyResult['rejected'];
      /** apply: the value each changed field held before the assistant's FIRST fill
       *  of it in this session — what an undo restores, and what the agent applies
       *  back when the user asks it to undo. */
      previous?: Record<string, unknown>;
      content?: string;
      detail?: string;
      /** navigate with `discard_changes`: the unsaved changes it left behind. */
      discarded?: string;
      /** invoke of an action marked `requiresApproval`: it saved its change. */
      saved?: true;
    }
  | {
      ok: false;
      reason:
        | 'invalid'
        | 'unknown-target'
        | 'unknown-action'
        | 'unsupported'
        | 'unsaved-changes'
        | 'failed';
      detail?: string;
    };

/**
 * Sandboxed executor of `urn:nannos:a2a:client-action` directives. It runs ONLY
 * against handles the host registered; an unknown target is refused, not guessed.
 *
 * There is NO confirm layer here: approval for an action that saves (marked
 * `requiresApproval`, e.g. a form's `save`) happens ONCE, upstream, at the agent's
 * tool-call HITL gate, and the run carries it (`deps.approved`). An `apply`
 * only writes into the unsaved form, so it runs without approval and its fields are
 * marked as changed (`markChanged`). The `confirm` field on a directive is ignored.
 */
export async function executeClientAction(
  raw: unknown,
  deps: ClientActionDeps,
): Promise<ClientActionResult> {
  const parsed = clientActionDirective.safeParse(raw);
  if (!parsed.success) return { ok: false, reason: 'invalid' };
  const directive = parsed.data;

  switch (directive.kind) {
    case 'apply': {
      const handle = deps.registry.get(directive.target.type, directive.target.id);
      if (!handle) return { ok: false, reason: 'unknown-target' };
      const fields = Object.keys(directive.values);
      const captured = deps.beforeApply?.(directive.target, fields);
      let previous: Record<string, unknown> = {};
      try {
        previous = ((handle.getState() ?? {}) as Record<string, unknown>) || {};
      } catch {
        /* no undo for this apply, the fill itself still runs */
      }
      const before: Record<string, unknown> = {};
      // Await: custom (plain-JS) handles may be async — a returned Promise must not
      // be mistaken for an ApplyResult. Sync returns pass through unchanged.
      const result = await handle.apply(directive.values);
      // apply may return void (custom handles) or an ApplyResult (zodFormRegistration);
      // shape-check rather than trust, since custom handles can return anything.
      if (
        result &&
        Array.isArray(result.applied) &&
        Array.isArray(result.rejected) &&
        (result.applied.length || result.rejected.length)
      ) {
        if (result.applied.length) {
          // Undo restores the user's own value: unvalidated where the form allows it.
          const write = async (values: Record<string, unknown>) => {
            const current = deps.registry.get(directive.target.type, directive.target.id);
            if (!current) return null;
            if (current.restore) {
              await current.restore(values);
              return Object.keys(values);
            }
            const r = await current.apply(values);
            return r && Array.isArray(r.applied) ? r.applied : Object.keys(values);
          };
          // A field set to the value it already had did not change: nothing to mark or undo.
          const changed = result.applied.filter(
            (field) => !sameValue(previous[field], (directive.values as Record<string, unknown>)[field]),
          );
          const changes =
            deps.changes && changed.length ? deps.changes.record(directive.target, previous, changed, write) : [];
          if (changes.length) deps.markChanged?.(directive.target, changes, captured);
          // The agent learns what it overwrote, so "undo that" is an apply of these
          // values — not a guess, and not a Page refresh (which keeps unsaved values).
          // A repeated fill reports the user's ORIGINAL value, the one undo restores.
          for (const change of changes) before[change.field] = change.previous;
          for (const field of changed) if (!(field in before) && field in previous) before[field] = previous[field];
          // A fill back to the user's original value IS the undo: the field is theirs
          // again, so its mark goes (an Undo that restores what is already there is noise).
          for (const change of changes) {
            if (sameValue(change.previous, (directive.values as Record<string, unknown>)[change.field])) change.dismiss();
          }
        }
        if (result.rejected.length) {
          if (deps.onApplyResult) deps.onApplyResult(directive.target, result);
          else
            console.warn(
              `[nannos] apply on ${directive.target.type}:${directive.target.id} rejected ` +
                `${result.rejected.length} field(s): ${result.rejected.map((r) => r.field).join(', ')}`,
            );
        }
        return {
          ok: true,
          applied: result.applied,
          rejected: result.rejected,
          // The values it overwrote are form content: only for a host that shares its
          // values with the agent (`includeValues`), like the manifest.
          ...(handle.includeValues && Object.keys(before).length ? { previous: before } : {}),
        };
      }
      return { ok: true };
    }
    case 'highlight': {
      if (!deps.registry.get(directive.target.type, directive.target.id))
        return { ok: false, reason: 'unknown-target' };
      deps.highlight?.(directive.target, directive.field);
      return { ok: true };
    }
    case 'invoke': {
      const handle = deps.registry.get(directive.target.type, directive.target.id);
      if (!handle) return { ok: false, reason: 'unknown-target' };
      const action = handle.actions?.[directive.action];
      if (!action) {
        const offered = Object.keys(handle.actions ?? {});
        const offers = offered.length ? `This object offers: ${offered.join(', ')}.` : 'This object offers no actions.';
        return {
          ok: false,
          reason: 'unknown-action',
          // A save approved after the form closed (a reload, the user left edit mode)
          // lands on the object's VIEW: the fill it was meant to save is gone, and
          // without saying so the agent told the user it was still on screen and to
          // press a Save button that does not exist.
          detail:
            directive.action === SAVE_ACTION && handle.scope === 'view'
              ? 'It is shown read-only right now: no form is open, so nothing was saved and no unsaved ' +
                `values are on screen. ${offers}`
              : offers,
        };
      }
      if (action.requiresApproval && !deps.approved) {
        return {
          ok: false,
          reason: 'failed',
          detail: 'This action saves, so it runs only after the user approves it; it was not run.',
        };
      }
      let outcome: { ok: boolean; detail?: string };
      try {
        outcome = normalizeOutcome(await action.run(directive.args ?? {}));
      } catch (err) {
        outcome = { ok: false, detail: err instanceof Error ? err.message : String(err) };
      }
      if (!outcome.ok) return { ok: false, reason: 'failed', ...(outcome.detail ? { detail: outcome.detail } : {}) };
      // It saved: whatever the assistant filled into this object is saved now, so its
      // marks go (a form's `save` is such an action).
      if (action.requiresApproval) {
        deps.changes?.clear(directive.target);
        deps.clearChanged?.(directive.target);
      }
      // An action usually opens something that registers its own form (a dialog, edit
      // mode): hand the agent the page as it settled, like a navigate.
      await settleRegistrations(deps.registry);
      return {
        ok: true,
        // Said, because by default an action saves nothing: told "nothing was saved"
        // after an approved "Set as default", the agent asked for it again.
        ...(action.requiresApproval ? { saved: true as const } : {}),
        ...(outcome.detail ? { detail: outcome.detail } : {}),
        content: await describeLandedPage(deps),
      };
    }
    case 'navigate': {
      if (!deps.navigate) return { ok: false, reason: 'unsupported' };
      // Leaving unmounts the forms, and with them their unsaved changes — the
      // assistant's fills and what the user typed alike. That is the user's call.
      const pending = deps.changes?.pending() ?? [];
      const filled = new Set(pending.map((u) => `${u.target.type}:${u.target.id}`));
      const unsaved = [
        ...pending.map((u) => `${u.target.type}:${u.target.id} (${u.fields.join(', ')})`),
        ...deps.registry
          .dirty()
          .filter((key) => !filled.has(key))
          .map((key) => `${key} (edits typed by the user)`),
      ].join('; ');
      if (unsaved && !directive.discard_changes) return { ok: false, reason: 'unsaved-changes', detail: unsaved };
      const refused = deps.navigate(directive.to);
      if (typeof refused === 'string') return { ok: false, reason: 'failed', detail: refused };
      // The agent's view of the page (page context, open forms) was taken when its
      // turn started. Hand it the page it landed on — once the new page has
      // registered its forms — or it navigates again and again, never seeing it arrive.
      await settleRegistrations(deps.registry);
      // Say what was thrown away, so the agent cannot report it as "filled, not saved".
      return { ok: true, ...(unsaved ? { discarded: unsaved } : {}), content: await describeLandedPage(deps) };
    }
    case 'read_current_page': {
      // The outline alone can answer — a host with no readers still has a screen.
      if (!deps.readCurrentPage && !deps.screenOutline) return { ok: false, reason: 'unsupported' };
      const raw = deps.readCurrentPage ? await deps.readCurrentPage() : {};
      const content = deps.screenOutline
        ? sanitizeReadResultWithScreen(raw, deps.screenOutline)
        : sanitizeReadResult(raw);
      return { ok: true, content };
    }
  }
}

/** Resolves once the page's object registrations have been quiet for `quietMs`
 *  (a new route mounts its forms over a few renders), or after `maxMs`. */
export function settleRegistrations(registry: ObjectRegistry, quietMs = 250, maxMs = 2000): Promise<void> {
  return new Promise((resolve) => {
    let quiet: ReturnType<typeof setTimeout>;
    const done = () => {
      clearTimeout(quiet);
      clearTimeout(cap);
      off();
      resolve();
    };
    const cap = setTimeout(done, maxMs);
    const off = registry.onChange(() => {
      clearTimeout(quiet);
      quiet = setTimeout(done, quietMs);
    });
    quiet = setTimeout(done, quietMs);
  });
}

/** Where the user is now and what they can act on — the snapshot a turn that just
 *  navigated has no other way to see. JSON, sized like the per-turn manifest: the
 *  merged page context (already sanitized by the provider) and the open objects. */
async function describeLandedPage(deps: ClientActionDeps): Promise<string> {
  let page: unknown = null;
  try {
    const answers = (await deps.readCurrentPage?.()) as { page?: unknown } | undefined;
    page = answers?.page ?? null;
  } catch {
    /* the objects alone still tell the agent where it is */
  }
  return JSON.stringify({ page, objects: deps.registry.manifest() });
}

function sameValue(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if ((a === undefined || a === null || a === '') && (b === undefined || b === null || b === '')) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

function normalizeOutcome(outcome: ActionOutcome): { ok: boolean; detail?: string } {
  if (outcome === false) return { ok: false };
  if (outcome && typeof outcome === 'object') return { ok: outcome.ok, detail: outcome.detail };
  return { ok: true };
}

/**
 * Build a wire directive from the agent's RAW TOOL ARGS — the flat, snake_case
 * shape the risk-gate approval card receives (`{ kind, target_type, target_id,
 * values, field, to }`), not the nested `{ target: { type, id } }` the directive
 * union uses.
 *
 * This is what lets ONE pause cover an approved `client_action`: the host runs
 * the directive the moment the user approves the card and returns the outcome on
 * the decision itself, instead of the agent resuming only to interrupt again for
 * the result. It mirrors `_client_action_handler` in `client_action_tool.py`;
 * `clientActionDirective` still validates the result, so a mismatch degrades to
 * `null` (the caller then approves plainly and the old two-pause path runs).
 */
export function directiveFromToolArgs(args: unknown): unknown | null {
  const a = args as Record<string, unknown> | null | undefined;
  const kind = a?.kind;
  if (typeof kind !== 'string') return null;
  const targetType = a?.target_type;
  const targetId = a?.target_id;
  const target =
    typeof targetType === 'string' && typeof targetId === 'string'
      ? { type: targetType, id: targetId }
      : null;

  switch (kind) {
    case 'apply':
      if (!target) return null;
      return { kind, target, values: a?.values ?? {} };
    case 'highlight':
      if (!target) return null;
      return { kind, target, ...(typeof a?.field === 'string' && { field: a.field }) };
    case 'navigate':
      return typeof a?.to === 'string'
        ? { kind, to: a.to, ...(a?.discard_changes === true && { discard_changes: true }) }
        : null;
    case 'invoke':
      if (!target || typeof a?.action !== 'string') return null;
      return {
        kind,
        target,
        action: a.action,
        ...(a?.args && typeof a.args === 'object' ? { args: a.args as Record<string, unknown> } : {}),
      };
    case 'read_current_page':
      return { kind };
    default:
      // An unknown kind is the agent's, not ours, to interpret: hand it back to
      // the round trip rather than guessing a directive shape for it.
      return null;
  }
}

/**
 * Unwrap a client-action directive from a raw `agent_response` event. Directives
 * ride status-update events tagged with `CLIENT_ACTION_EXT`, nested at
 * `status.message.parts[].data.directive` — they never appear at the top level.
 * Returns null for every other event (including streaming text chunks), so
 * callers can bail before any schema validation.
 */
export function extractClientActionDirective(data: unknown): unknown | null {
  const evt = data as {
    kind?: string;
    status?: {
      message?: {
        extensions?: string[];
        parts?: Array<{ kind?: string; data?: Record<string, unknown> }>;
      };
    };
  };
  if (evt?.kind !== 'status-update') return null;
  const msg = evt.status?.message;
  if (!msg?.extensions?.includes(CLIENT_ACTION_EXT)) return null;
  const part = msg.parts?.find((p) => p.kind === 'data' || p.data);
  return (part?.data as { directive?: unknown } | undefined)?.directive ?? null;
}
