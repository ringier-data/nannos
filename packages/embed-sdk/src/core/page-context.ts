/**
 * What the host's pages tell the assistant about themselves — the LIVE
 * "where is the user now" that rides every send as `metadata.pageContext`.
 *
 * The router only gives a path, and a page title only gives a display name.
 * Neither resolves "this campaign" to an id the agent's tools accept, and
 * neither says which tab is open. Pages fill that gap by declaring layers
 * (`useNannosPageContext`); the provider folds them into one snapshot.
 *
 * Everything here reaches a model, so a page declares NAMED fields rather
 * than spreading a fetched object into them. `sanitizePageContext` is the
 * second line of defence, not the first: per-field caps, a secret-key deny
 * list, and a whole-payload ceiling so a page cannot spend the conversation's
 * context on its own state.
 *
 * Adapted from Gatana's battle-tested assistant/page-context.ts (the PUSH
 * half; the on-demand page READER stays parked — see PENDING #4).
 */

/** The thing the page is about, in the host's own ontology. */
export interface NannosPageEntity {
  /** Host ontology type, e.g. 'Campaign' — ideally the same string the object
   *  registry uses, so the agent can connect the two. */
  type: string;
  id: string;
  name?: string;
}

/**
 * One layer of page context. The host's router bridge publishes the base
 * (key + title); a page, tab or dialog layers what only it knows on top.
 */
export interface NannosPageContext {
  /**
   * Stable identity of the page, e.g. a route path (`/campaigns/123`). Also
   * the default conversation-scoping `contextKey` for prompts seeded through
   * `open()`. Usually only the base (router) layer sets it — the merged
   * snapshot is published only while SOME layer provides one.
   */
  key?: string;
  /** What the page is about, in the words the user would use. */
  title?: string;
  /** Where the page sits in the app's hierarchy. */
  breadcrumbs?: string[];
  /** The thing on screen, so "this campaign" resolves to an id tools accept. */
  entity?: NannosPageEntity;
  /** Active tab, filter or selection. Scalars only; a key that reads as a
   *  secret is dropped. */
  view?: Record<string, string | number | boolean | null | undefined>;
  /** Names of what the user can see, so "the second one" can be resolved. */
  visible?: string[];
}

const MAX_KEY = 500;
const MAX_TITLE = 160;
const MAX_ENTITY_ID = 200;
const MAX_BREADCRUMBS = 8;
const MAX_BREADCRUMB = 120;
/** Enough for a page and a tab to describe themselves together. */
const MAX_VIEW_KEYS = 16;
const MAX_VIEW_KEY = 60;
const MAX_VIEW_VALUE = 200;
const MAX_VISIBLE = 25;
const MAX_VISIBLE_ENTRY = 120;
/** Whole-payload ceiling — it rides EVERY send. */
const MAX_SERIALIZED = 2000;

/**
 * Keys whose value must never leave the browser. A page should not put a
 * secret in the context at all, but a rename or a spread would otherwise carry
 * one to the model without anyone noticing.
 *
 * Only words that name a secret itself — words that merely describe one
 * (`hasCredentials`, `authMethod`) are exactly the state the assistant needs.
 */
const SECRET_KEY = /secret|token|password|passphrase|api[-_]?key|private[-_]?key|bearer/i;

function clamp(value: string, max: number): string {
  const trimmed = value.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

/**
 * Fold the registered layers into one snapshot. A later layer wins, so a tab
 * or dialog describes the screen more precisely than the page that contains
 * it. `view` is merged key by key rather than replaced, so a tab can add to
 * what the page already said.
 */
export function mergePageContexts(layers: readonly NannosPageContext[]): NannosPageContext {
  const merged: NannosPageContext = {};
  for (const layer of layers) {
    if (layer.key) merged.key = layer.key;
    if (layer.title) merged.title = layer.title;
    if (layer.breadcrumbs) merged.breadcrumbs = layer.breadcrumbs;
    if (layer.entity) merged.entity = layer.entity;
    if (layer.visible) merged.visible = layer.visible;
    if (layer.view) merged.view = { ...merged.view, ...layer.view };
  }
  return merged;
}

/**
 * Apply the caps and the deny list, and drop what a model cannot use.
 * Returns null while no layer provides a `key` — the snapshot is anchored to
 * the page's identity, and without one there is nothing to scope or show.
 */
export function sanitizePageContext(payload: NannosPageContext): NannosPageContext | null {
  if (!payload.key?.trim()) return null;
  const sanitized: NannosPageContext = { key: clamp(payload.key, MAX_KEY) };

  if (payload.title?.trim()) {
    sanitized.title = clamp(payload.title, MAX_TITLE);
  }

  const breadcrumbs = (payload.breadcrumbs ?? [])
    .filter((label) => typeof label === 'string' && label.trim())
    .slice(0, MAX_BREADCRUMBS)
    .map((label) => clamp(label, MAX_BREADCRUMB));
  if (breadcrumbs.length) {
    sanitized.breadcrumbs = breadcrumbs;
  }

  if (payload.entity?.type && payload.entity.id) {
    sanitized.entity = {
      type: clamp(String(payload.entity.type), MAX_VIEW_KEY),
      id: clamp(String(payload.entity.id), MAX_ENTITY_ID),
      ...(payload.entity.name ? { name: clamp(payload.entity.name, MAX_TITLE) } : {}),
    };
  }

  const view: Record<string, string | number | boolean> = {};
  // Over the cap, the LAST keys are kept: a merged payload holds the page's
  // keys first and the tab's or dialog's after them, so what survives is the
  // most specific description of the screen.
  for (const [key, value] of Object.entries(payload.view ?? {}).slice(-MAX_VIEW_KEYS)) {
    if (SECRET_KEY.test(key)) continue;
    if (typeof value === 'number' || typeof value === 'boolean') {
      view[clamp(key, MAX_VIEW_KEY)] = value;
    } else if (typeof value === 'string' && value.trim()) {
      view[clamp(key, MAX_VIEW_KEY)] = clamp(value, MAX_VIEW_VALUE);
    }
  }
  if (Object.keys(view).length) {
    sanitized.view = view;
  }

  const visible = (payload.visible ?? [])
    .filter((entry) => typeof entry === 'string' && entry.trim())
    .slice(0, MAX_VISIBLE)
    .map((entry) => clamp(entry, MAX_VISIBLE_ENTRY));
  if (visible.length) {
    sanitized.visible = visible;
  }

  // Shed the bulky fields before the small ones, so what is left still
  // identifies the page.
  if (JSON.stringify(sanitized).length > MAX_SERIALIZED) {
    delete sanitized.visible;
  }
  if (JSON.stringify(sanitized).length > MAX_SERIALIZED) {
    delete sanitized.view;
  }
  return sanitized;
}
