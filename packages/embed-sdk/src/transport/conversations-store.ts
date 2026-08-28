/**
 * The conversation LIST and its selection — the thin remainder of the old
 * ChatContext once messages moved into `useChat`: server-authoritative list,
 * keyset pagination cursors, per-sub-agent scoping, session resume, contextKey
 * bookkeeping, unread counts. Framework-free store consumed via
 * subscribe/getSnapshot (useSyncExternalStore).
 *
 * Semantics ported from ChatContext.tsx:1010-1122 (list), :1412-1440 (create),
 * :56-101 + :1902-1946 (session resume + contextKey), :1943-1946 (read-only).
 */
import { generateUUID } from '../core/protocol';

/** Where a conversation STARTED, as the backend stamped it at creation. */
export interface ConversationOrigin {
  /** The page's stable key, e.g. '/campaigns/123'. */
  key?: string;
  /** What that page was called, e.g. 'Campaign 42'. */
  title?: string;
  /** The thing the page was about — 'Campaign' + '123' + optional name. */
  entity?: { type: string; id: string; name?: string };
}

export interface ConversationMeta {
  id: string;
  /** The conversation's name, or '' when it has none yet. NEVER a placeholder:
   *  the UI renders the untitled case through the strings table, so the store
   *  holds no English. */
  title: string;
  lastMessage: string;
  /** One sentence on what the conversation is about, written by the backend
   *  after the first exchange. Absent until then (and on older rows). */
  summary?: string;
  /** The page the conversation was started from, when the host published one. */
  origin?: ConversationOrigin;
  /** ISO time of last activity (server value when known). */
  updatedAt: string;
  status: 'active' | 'archived';
  hasActiveTasks: boolean;
  /** Set when the conversation belongs to an embedded sub-agent surface. */
  embeddedSubAgentId?: string;
  unread: number;
}

export interface ConversationsSnapshot {
  items: ConversationMeta[];
  activeId: string | null;
  isLoading: boolean;
}

export interface ConversationsStoreOptions {
  fetch: (path: string, init?: RequestInit) => Promise<Response>;
  /** Execute-only embed scoping (ADR-0004); also scopes the session-resume record. */
  subAgentId?: string | number;
  /** Playground scoping (console sub-agent playground). */
  subAgentConfigHash?: string;
  /** Filter by orchestrator URL (console passes its configured agent). */
  getAgentUrl?: () => string | undefined;
  /** Console behavior: adopt the most recent conversation when none is active.
   *  Embedded surfaces pass false and start fresh instead. Either way, the
   *  conversation the TAB was on wins first (see `resumeInto`). */
  autoSelectConversation?: boolean;
}

/** The longest name the rename endpoint accepts — the same ceiling the backend
 *  titler writes to, so a renamed conversation sits in the list like any other. */
export const MAX_CONVERSATION_TITLE = 60;

/**
 * Which conversation this browser TAB was last on. sessionStorage on purpose:
 * a reload of the tab lands back where the user was, a brand-new tab starts
 * clean, and nothing leaks between tabs looking at different pages.
 *
 * The key is scoped so surfaces never resume each other's conversation:
 * a playground by its config hash, an embedded widget by its sub-agent id,
 * and every other surface (the console's own panel) under 'default'. The
 * sub-agent form is unchanged from when only embedded surfaces resumed.
 */
const sessionKey = (scope: string) => `nannos-active-conversation:${scope}`;

function resolveSessionScope(opts: ConversationsStoreOptions): string {
  if (opts.subAgentConfigHash) return `playground:${opts.subAgentConfigHash}`;
  if (opts.subAgentId !== undefined) return String(opts.subAgentId);
  return 'default';
}

interface SessionConversation {
  id: string;
  contextKey?: string;
}

function readSessionConversation(scope: string): SessionConversation | null {
  try {
    const raw = sessionStorage.getItem(sessionKey(scope));
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as SessionConversation;
      return typeof parsed?.id === 'string' ? parsed : null;
    } catch {
      return { id: raw }; // pre-JSON value from an older build
    }
  } catch {
    return null;
  }
}

export class ConversationsStore {
  private snapshot: ConversationsSnapshot = { items: [], activeId: null, isLoading: false };
  private readonly listeners = new Set<() => void>();
  /** Keyset pagination per conversation: cursor for the NEXT older page. */
  private readonly pages = new Map<string, { cursor: string | null; hasMore: boolean }>();
  private readonly contextKeys = new Map<string, string>();
  /** Ids minted HERE that the server has never listed — nothing to fetch or
   *  resume for them, and `subscribe_conversation` is rejected outright. */
  private readonly localOnly = new Set<string>();
  /** Storage scope for this surface's "last conversation" record. */
  private readonly sessionScope: string;
  /**
   * The conversation this tab was on before the reload, read ONCE at
   * construction. It has to be held in memory because the panel mints and
   * adopts a blank conversation while the first list fetch is still in flight
   * — that write would otherwise overwrite the record we are about to read.
   *
   * Cleared as soon as the first list settles, or the moment the user picks a
   * conversation themselves: a later refresh of the list (a background turn
   * finishing, a search) must never yank the user back to where they started.
   */
  private pendingResume: SessionConversation | null;
  /** The blank conversation the panel minted for want of an active one. Only
   *  this one may be replaced by the resumed conversation. */
  private autoAdoptedId: string | null = null;

  constructor(private readonly opts: ConversationsStoreOptions) {
    this.sessionScope = resolveSessionScope(opts);
    this.pendingResume = readSessionConversation(this.sessionScope);
  }

  getSnapshot = (): ConversationsSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(patch: Partial<ConversationsSnapshot>) {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const l of this.listeners) l();
  }

  get activeId(): string | null {
    return this.snapshot.activeId;
  }

  // --- list -----------------------------------------------------------------

  async loadList(search?: string): Promise<void> {
    this.set({ isLoading: true });
    try {
      const params = new URLSearchParams();
      params.set('limit', '50');
      if (search?.trim()) params.set('search', search.trim());
      const agentUrl = this.opts.getAgentUrl?.();
      if (agentUrl) params.set('agent_url', agentUrl);
      if (this.opts.subAgentConfigHash) {
        params.set('sub_agent_config_hash', this.opts.subAgentConfigHash);
      } else {
        params.set('exclude_playground', 'true');
      }
      // Embedded widget: scope server-side — a host page must only ever receive
      // its own conversations.
      if (this.opts.subAgentId !== undefined) {
        params.set('embedded_sub_agent_id', String(this.opts.subAgentId));
      }

      const resp = await this.opts.fetch(`/api/v1/conversations/?${params.toString()}`);
      if (!resp.ok) throw new Error(`Failed to load conversations (status=${resp.status})`);
      const data = (await resp.json()) as Record<string, unknown>;
      const raw = Array.isArray(data.items)
        ? (data.items as Record<string, unknown>[])
        : Array.isArray(data.conversations)
          ? (data.conversations as Record<string, unknown>[])
          : [];

      const prevUnread = new Map(this.snapshot.items.map((c) => [c.id, c.unread]));
      const mapped: ConversationMeta[] = raw.map((c) => {
        const id = (c.id ?? c.conversation_id ?? c.conversationId) as string;
        const ts = (c.last_message_at ??
          c.lastMessageAt ??
          c.last_updated ??
          c.lastUpdated ??
          c.updated_at ??
          c.started_at ??
          c.created_at ??
          null) as string | null;
        const meta = (c.metadata ?? {}) as Record<string, unknown>;
        const embedded = meta.embedded_sub_agent_id;
        const summary = typeof meta.summary === 'string' ? meta.summary.trim() : '';
        const origin = readOrigin(meta.page_context);
        return {
          id,
          title: ((c.title ?? meta.title) as string) || '',
          lastMessage: ((c.last_message ?? c.lastMessage) as string) || '',
          updatedAt: ts ?? new Date().toISOString(),
          status: ((c.status as 'active' | 'archived') ?? 'active') || 'active',
          hasActiveTasks: !!c.has_active_tasks,
          ...(summary && { summary }),
          ...(origin && { origin }),
          ...(embedded != null && { embeddedSubAgentId: String(embedded) }),
          unread: prevUnread.get(id) ?? 0,
        };
      });

      // A conversation created locally this render-cycle (an injected prompt
      // racing this fetch) isn't persisted yet — keep it, or the view yanks.
      const activeId = this.snapshot.activeId;
      const activeLocal = activeId ? this.snapshot.items.find((c) => c.id === activeId) : undefined;
      const items =
        activeLocal && !mapped.some((c) => c.id === activeId) ? [activeLocal, ...mapped] : mapped;
      for (const c of mapped) this.localOnly.delete(c.id);
      this.set({ items });

      // Selection, decided on the FIRST list this store ever loads: the
      // conversation the tab was on before the reload wins over everything
      // else — that is what makes a refresh a no-op for the user. Failing
      // that, the console adopts the most recent chat and an embedded widget
      // starts fresh. Every later refresh of the list leaves the selection
      // alone (`pendingResume` is spent below).
      const resume = this.pendingResume;
      this.pendingResume = null;
      const resumed = resume && items.find((c) => c.id === resume.id);
      if (resume && resumed) {
        if (resume.contextKey) this.contextKeys.set(resumed.id, resume.contextKey);
        this.resumeInto(resumed.id);
      } else if (
        !this.snapshot.activeId &&
        items.length > 0 &&
        (this.opts.autoSelectConversation ?? true)
      ) {
        this.set({ activeId: items[0].id });
      }
    } catch (e) {
      console.warn('[nannos] loadConversations failed', e);
    } finally {
      this.set({ isLoading: false });
    }
  }

  /**
   * Remove a conversation from the user's history. The server soft-deletes it
   * (DELETE → status 'archived'), so the row and its messages survive — it just
   * stops being listed.
   *
   * Optimistic: the row leaves the list at once and comes back if the request
   * fails, because the list is the only feedback the user gets. A conversation
   * that only ever existed here (`localOnly`) has nothing to delete server-side.
   *
   * Deleting the ACTIVE conversation lands on a fresh chat rather than on no
   * selection at all — `useNannosChat` mints and re-adopts its own id whenever
   * nothing is active, which would resurrect the row that was just deleted.
   */
  async remove(id: string): Promise<boolean> {
    if (!this.snapshot.items.some((c) => c.id === id)) return false;
    const prevItems = this.snapshot.items;
    const prevActiveId = this.snapshot.activeId;
    const wasActive = prevActiveId === id;

    const items = prevItems.filter((c) => c.id !== id);
    let replacementId: string | null = null;
    if (wasActive) {
      replacementId = generateUUID();
      this.localOnly.add(replacementId);
      items.unshift(blankConversation(replacementId));
    }
    this.set({ items, ...(wasActive && { activeId: replacementId }) });

    const wasLocalOnly = this.localOnly.delete(id);
    if (!wasLocalOnly) {
      try {
        const resp = await this.opts.fetch(`/api/v1/conversations/${encodeURIComponent(id)}`, {
          method: 'DELETE',
        });
        // 404 means it is already gone for this user — the optimistic removal
        // was right, so treat it as success rather than putting the row back.
        if (!resp.ok && resp.status !== 404) {
          throw new Error(`Failed to delete conversation (status=${resp.status})`);
        }
      } catch (e) {
        console.warn('[nannos] deleteConversation failed', e);
        // `id` was NOT local-only on this path, so there is nothing to put
        // back in that set — only the replacement chat has to be unwound.
        if (replacementId) this.localOnly.delete(replacementId);
        this.set({ items: prevItems, activeId: prevActiveId });
        return false;
      }
    }

    this.pages.delete(id);
    this.contextKeys.delete(id);
    // The replacement chat exists only in this browser: a session record
    // pointing at it could never be resumed after a reload, so drop the record
    // and let the next load land on the most recent conversation instead.
    if (wasActive) this.clearSession();
    return true;
  }

  /**
   * Give a conversation the name the user typed.
   *
   * Optimistic like `remove`, and for the same reason: the list is the only
   * feedback there is. The row goes back to its old name if the server refuses.
   *
   * The name is cleaned exactly as the endpoint cleans it (whitespace collapsed,
   * capped), so the row shows what was actually stored rather than a version
   * that differs from it by a space.
   *
   * Returns false when the name could not be stored — nothing to store, an
   * unknown id, or a refused request.
   */
  async rename(id: string, title: string): Promise<boolean> {
    const clean = title.replace(/\s+/g, ' ').trim().slice(0, MAX_CONVERSATION_TITLE);
    const target = this.snapshot.items.find((c) => c.id === id);
    if (!clean || !target) return false;
    // Nothing changed: the user opened the editor and left the name alone.
    if (target.title === clean) return true;
    const previousTitle = target.title;

    const applyTitle = (value: string) =>
      this.set({
        items: this.snapshot.items.map((c) => (c.id === id ? { ...c, title: value } : c)),
      });
    applyTitle(clean);

    try {
      const resp = await this.opts.fetch(`/api/v1/conversations/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: clean }),
      });
      // A conversation started in this browser that has never sent a message has
      // no server row to name yet — a 404 there is expected, and the local name
      // stands until the first turn creates the row.
      if (!resp.ok && !(resp.status === 404 && this.localOnly.has(id))) {
        throw new Error(`Failed to rename conversation (status=${resp.status})`);
      }
    } catch (e) {
      console.warn('[nannos] renameConversation failed', e);
      // Only the title is put back — a turn may have touched the row's unread
      // count or preview while the request was in flight.
      applyTitle(previousTitle);
      return false;
    }
    return true;
  }

  // --- selection --------------------------------------------------------------

  select(id: string | null): void {
    // The user is steering: no session record may move them afterwards.
    this.pendingResume = null;
    if (id === this.snapshot.activeId) return;
    this.set({
      activeId: id,
      items: this.snapshot.items.map((c) => (c.id === id ? { ...c, unread: 0 } : c)),
    });
    this.persistSession();
  }

  /** Register + select an id minted elsewhere (render-time), once. */
  adopt(id: string, contextKey?: string): void {
    if (this.snapshot.items.some((c) => c.id === id)) {
      if (this.snapshot.activeId !== id) this.select(id);
      return;
    }
    // Minted by the panel for want of an active conversation, NOT chosen by the
    // user — so a session resume still arriving may replace it.
    this.autoAdoptedId = id;
    this.insert(id, contextKey);
  }

  /** Create a local conversation (uuidv7 = the wire contextId) and select it. */
  create(contextKey?: string): string {
    const id = generateUUID();
    // A new chat is the user's own choice: nothing may pull them out of it.
    this.pendingResume = null;
    this.autoAdoptedId = null;
    this.insert(id, contextKey);
    return id;
  }

  /**
   * Land on the conversation this tab was on before the reload.
   *
   * The panel mints and adopts a blank conversation the moment it renders with
   * nothing selected, which usually beats the list fetch home — so resuming
   * means quietly dropping that empty placeholder. A conversation the user has
   * already spoken into is never taken away from them.
   */
  private resumeInto(id: string): void {
    const active = this.snapshot.activeId;
    if (active === id) return;
    if (active !== null) {
      const placeholder =
        active === this.autoAdoptedId &&
        this.localOnly.has(active) &&
        this.snapshot.items.some((c) => c.id === active && !c.title && !c.lastMessage);
      if (!placeholder) return;
      this.localOnly.delete(active);
      this.pages.delete(active);
      this.contextKeys.delete(active);
      this.autoAdoptedId = null;
      this.set({ items: this.snapshot.items.filter((c) => c.id !== active) });
    }
    this.set({
      items: this.snapshot.items.map((c) => (c.id === id ? { ...c, unread: 0 } : c)),
      activeId: id,
    });
    this.persistSession();
  }

  private insert(id: string, contextKey?: string): void {
    const meta = blankConversation(id);
    if (contextKey) this.contextKeys.set(id, contextKey);
    this.localOnly.add(id);
    this.set({ items: [meta, ...this.snapshot.items], activeId: id });
    this.persistSession();
  }

  /**
   * Resolve the conversation a (possibly keyed) prompt should land in:
   * same key → continue the active conversation; different/new key → fresh.
   * (ChatContext.tsx:1921-1938 semantics.)
   *
   * `fresh` (an `open({newConversation: true})` seed) always lands in a NEW
   * conversation — except when the active one is still blank (local-only, no
   * title, no message): that IS a new conversation, and creating another would
   * both litter the list and, called again from a re-rendering effect, loop.
   */
  resolveTarget(contextKey?: string, opts?: { fresh?: boolean }): string {
    const activeId = this.snapshot.activeId;
    if (opts?.fresh) {
      if (activeId && this.isBlank(activeId)) {
        if (contextKey) this.contextKeys.set(activeId, contextKey);
        return activeId;
      }
      return this.create(contextKey);
    }
    if (!activeId) return this.create(contextKey);
    if (contextKey === undefined) return activeId;
    const activeKey = this.contextKeys.get(activeId);
    return activeKey === contextKey ? activeId : this.create(contextKey);
  }

  /** Created in this browser and never written to — same placeholder test the
   *  auto-adopt path uses in `select()`. */
  private isBlank(id: string): boolean {
    return (
      this.localOnly.has(id) &&
      this.snapshot.items.some((c) => c.id === id && !c.title && !c.lastMessage)
    );
  }

  /** True while this conversation exists only in this browser — created here
   *  and not yet returned by the server list. */
  isLocalOnly(id: string): boolean {
    return this.localOnly.has(id);
  }

  contextKeyOf(id: string): string | undefined {
    return this.contextKeys.get(id);
  }

  /** A conversation owned by ANOTHER embedded surface renders read-only here. */
  isReadOnly(id: string): boolean {
    const conversation = this.snapshot.items.find((c) => c.id === id);
    if (!conversation?.embeddedSubAgentId) return false;
    return String(this.opts.subAgentId ?? '') !== conversation.embeddedSubAgentId;
  }

  private persistSession(): void {
    const activeId = this.snapshot.activeId;
    if (!activeId) return;
    try {
      sessionStorage.setItem(
        sessionKey(this.sessionScope),
        JSON.stringify({ id: activeId, contextKey: this.contextKeys.get(activeId) }),
      );
    } catch {
      /* resume simply won't survive a reload */
    }
  }

  private clearSession(): void {
    try {
      sessionStorage.removeItem(sessionKey(this.sessionScope));
    } catch {
      /* nothing to forget */
    }
  }

  // --- turn-event feedback (unread, previews) ---------------------------------

  noteActivity(conversationId: string, preview?: string): void {
    this.set({
      items: this.snapshot.items.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              ...(preview && { lastMessage: preview.slice(0, 50) }),
              updatedAt: new Date().toISOString(),
              unread: conversationId === this.snapshot.activeId ? 0 : c.unread + 1,
            }
          : c,
      ),
    });
  }

  /**
   * The first user message names the conversation, until the backend replaces it
   * with a written title (ChatContext.tsx:1678-1686). Only an UNTITLED
   * conversation is touched — never a name the server already gave us.
   */
  noteTitle(conversationId: string, title: string): void {
    const trimmed = title.slice(0, 40) + (title.length > 40 ? '…' : '');
    this.set({
      items: this.snapshot.items.map((c) =>
        c.id === conversationId && !c.title ? { ...c, title: trimmed } : c,
      ),
    });
  }

  /**
   * The backend finished naming a conversation and pushed it over the socket
   * (it writes a title + summary once the first exchange completes). This
   * OVERWRITES the local first-message title — that was the placeholder.
   */
  applyServerTitle(conversationId: string, patch: { title?: string; summary?: string }): void {
    const title = patch.title?.trim();
    const summary = patch.summary?.trim();
    if (!title && !summary) return;
    let changed = false;
    const items = this.snapshot.items.map((c) => {
      if (c.id !== conversationId) return c;
      if (title === c.title && summary === c.summary) return c;
      changed = true;
      return { ...c, ...(title && { title }), ...(summary && { summary }) };
    });
    if (changed) this.set({ items });
  }

  // --- message pagination cursors ---------------------------------------------

  pageState(conversationId: string): { cursor: string | null; hasMore: boolean } {
    // No cursor means nothing to load — hasMore only turns true once a fetch
    // returns a next_cursor.
    return this.pages.get(conversationId) ?? { cursor: null, hasMore: false };
  }

  setPageState(conversationId: string, state: { cursor: string | null; hasMore: boolean }): void {
    this.pages.set(conversationId, state);
  }
}

/** A conversation that exists only here so far — no server row behind it yet. */
function blankConversation(id: string): ConversationMeta {
  return {
    id,
    title: '',
    lastMessage: '',
    updatedAt: new Date().toISOString(),
    status: 'active',
    hasActiveTasks: false,
    unread: 0,
  };
}

/** Read the `page_context` stamp off a conversation's metadata. Defensive: the
 *  row may predate the stamp, or carry a shape an older backend wrote. */
function readOrigin(raw: unknown): ConversationOrigin | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const data = raw as Record<string, unknown>;
  const origin: ConversationOrigin = {};
  if (typeof data.key === 'string' && data.key.trim()) origin.key = data.key;
  if (typeof data.title === 'string' && data.title.trim()) origin.title = data.title;
  const entity = data.entity as Record<string, unknown> | undefined;
  if (entity && typeof entity.type === 'string' && typeof entity.id === 'string') {
    origin.entity = {
      type: entity.type,
      id: entity.id,
      ...(typeof entity.name === 'string' && entity.name.trim() && { name: entity.name }),
    };
  }
  return Object.keys(origin).length > 0 ? origin : undefined;
}
