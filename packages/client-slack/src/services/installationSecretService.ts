/**
 * InstallationSecretService
 * -------------------------
 * Abstract per-installation notification-secret service. Concrete subclasses
 * implement `resolve(installationId)` against a particular backend (AWS SSM,
 * GCP Secret Manager, Vault, ...). The abstract base provides an in-memory
 * Promise cache so callers can invoke `getOrCreate` repeatedly without
 * re-hitting the backend.
 *
 * Implementations:
 *   - AwsSsmInstallationSecretService — see ./awsSsmInstallationSecretService.ts
 *
 * The same module is duplicated verbatim in client-google-chat — keep them in sync.
 */

// How long a "no secret provisioned" result is remembered before the backend is
// probed again. Bounds backend reads when an unknown/invalid notification token
// repeatedly hits the callback (every request scans all installations); without it
// each request re-reads the backend for every not-yet-provisioned installation.
// Kept short so a freshly-registered secret still becomes visible without restart.
const NEGATIVE_CACHE_TTL_MS = 60_000;

export abstract class InstallationSecretService {
  private readonly cache = new Map<string, string>();
  private readonly inflightRead = new Map<string, Promise<string | null>>();
  // installationId → epoch ms at which the remembered miss expires.
  private readonly negativeCache = new Map<string, number>();

  /**
   * Resolve the notification secret for `installationId`, generating and
   * persisting one if it does not yet exist. Successful results are cached
   * for the process lifetime. Intended to be called serially at startup.
   */
  async getOrCreate(installationId: string): Promise<string> {
    const cached = this.cache.get(installationId);
    if (cached) return cached;
    const value = await this.resolve(installationId);
    this.cache.set(installationId, value);
    return value;
  }

  /**
   * Read-only lookup. Returns the existing secret for `installationId`, or
   * `null` if none has been provisioned. Successful reads are cached for the
   * process lifetime; misses are cached only briefly (NEGATIVE_CACHE_TTL_MS) so
   * a later registration still becomes visible without restart while repeated
   * lookups for an unprovisioned id don't re-hit the backend every request.
   * Concurrent callers share a single in-flight backend request.
   */
  async get(installationId: string): Promise<string | null> {
    const cached = this.cache.get(installationId);
    if (cached) return cached;

    const missUntil = this.negativeCache.get(installationId);
    if (missUntil !== undefined) {
      if (missUntil > Date.now()) return null;
      this.negativeCache.delete(installationId);
    }

    let pending = this.inflightRead.get(installationId);
    if (!pending) {
      pending = (async () => {
        try {
          const value = await this.read(installationId);
          if (value !== null) {
            this.cache.set(installationId, value);
          } else {
            this.negativeCache.set(installationId, Date.now() + NEGATIVE_CACHE_TTL_MS);
          }
          return value;
        } finally {
          this.inflightRead.delete(installationId);
        }
      })();
      this.inflightRead.set(installationId, pending);
    }
    return pending;
  }

  /** Backend-specific get-or-create: read if present, otherwise generate, persist, and return. */
  protected abstract resolve(installationId: string): Promise<string>;

  /** Backend-specific read-only lookup; returns `null` when no secret exists. */
  protected abstract read(installationId: string): Promise<string | null>;
}

