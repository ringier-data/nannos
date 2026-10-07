/**
 * What the assistant changed on screen and has not been saved, reviewed or taken
 * back yet — one entry per field an `apply` wrote. Fills run without asking, so the
 * user's review happens here: every entry knows the value it replaced and can put it
 * back. The DOM marks (`createClientActionHandlers`) and a host's review bar
 * (`useNannosChanges`) both read from this one store.
 */

export interface ChangeTarget {
  type: string;
  id: string;
}

export interface AppliedChange {
  target: ChangeTarget;
  field: string;
  /** The value the field held before the assistant wrote it, as the form reports it. */
  previous: unknown;
  /** Write `previous` back through the form's own adapter, then drop the entry.
   *  Resolves false when the form refused the old value (or is gone). */
  undo: () => Promise<boolean>;
  /** Drop the entry without touching the form — the user took the field over. */
  dismiss: () => void;
  /** Called once when the entry goes away (undone, dismissed, saved, unmounted). */
  onDismiss: (fn: () => void) => void;
}

const keyOf = (target: ChangeTarget) => `${target.type}:${target.id}`;

export class ChangeStore {
  private readonly entries = new Map<string, Map<string, AppliedChange>>();
  private readonly listeners = new Set<() => void>();
  private version = 0;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Monotonic stamp for `useSyncExternalStore` snapshots. */
  getVersion = (): number => this.version;

  get(target: ChangeTarget): AppliedChange[] {
    return [...(this.entries.get(keyOf(target))?.values() ?? [])];
  }

  /**
   * Record the fields an apply wrote. `write` is the form's apply, used for undo.
   * A field written again keeps its ORIGINAL previous value: undo returns to what
   * the user had, not to the assistant's earlier guess.
   */
  record(
    target: ChangeTarget,
    previous: Record<string, unknown>,
    fields: string[],
    write: (values: Record<string, unknown>) => Promise<string[] | null>,
  ): AppliedChange[] {
    const key = keyOf(target);
    if (!this.entries.has(key)) this.entries.set(key, new Map());
    const forTarget = this.entries.get(key)!;
    const recorded: AppliedChange[] = [];
    for (const field of fields) {
      const existing = forTarget.get(field);
      if (existing) {
        recorded.push(existing);
        continue;
      }
      const callbacks: Array<() => void> = [];
      let gone = false;
      const dismiss = () => {
        if (gone) return;
        gone = true;
        if (forTarget.get(field) === change) forTarget.delete(field);
        if (!forTarget.size) this.entries.delete(key);
        for (const fn of callbacks.splice(0)) fn();
        this.bump();
      };
      const change: AppliedChange = {
        target: { ...target },
        field,
        previous: previous[field],
        undo: async () => {
          if (gone) return false;
          const applied = await write({ [field]: previous[field] }).catch(() => null);
          if (applied && !applied.includes(field)) return false;
          dismiss();
          return true;
        },
        dismiss,
        onDismiss: (fn) => {
          if (gone) fn();
          else callbacks.push(fn);
        },
      };
      forTarget.set(field, change);
      recorded.push(change);
    }
    this.bump();
    return recorded;
  }

  /** Every target with unsaved assistant changes, and which fields. */
  pending(): Array<{ target: ChangeTarget; fields: string[] }> {
    return [...this.entries.values()]
      .filter((forTarget) => forTarget.size)
      .map((forTarget) => {
        const changes = [...forTarget.values()];
        return { target: { ...changes[0].target }, fields: changes.map((c) => c.field) };
      });
  }

  /** Drop every entry of a target (it was saved). */
  clear(target: ChangeTarget): void {
    for (const change of this.get(target)) change.dismiss();
  }

  /** Drop entries whose object is no longer on screen. */
  prune(isRegistered: (target: ChangeTarget) => boolean): void {
    for (const forTarget of [...this.entries.values()]) {
      for (const change of [...forTarget.values()]) {
        if (!isRegistered(change.target)) change.dismiss();
      }
    }
  }

  private bump() {
    this.version += 1;
    for (const fn of this.listeners) fn();
  }
}
