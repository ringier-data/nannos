/**
 * A one-shot "focus the composer" signal — a counter the composer watches.
 *
 * The new-chat buttons (header and sidebar) fire it: a fresh conversation wants
 * the caret in the input, and any leftover draft SELECTED, so the user's next
 * keystroke replaces it. Kept out of the conversation store on purpose — it is
 * a UI intent, not conversation state, and it must NOT fire for the `create()`
 * calls that resolve a seeded prompt's target.
 */
export class ComposerFocusSignal {
  private count = 0;
  private readonly listeners = new Set<() => void>();

  getSnapshot = (): number => this.count;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  request = (): void => {
    this.count += 1;
    for (const l of this.listeners) l();
  };
}
