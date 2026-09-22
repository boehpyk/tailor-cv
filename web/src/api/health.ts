import { request } from './client';

/**
 * Mirrors `ReadinessResponse` in the API's `schemas/health.py`.
 *
 * Hand-written for now, and that is a known seam: two declarations of one contract drift. When the
 * API surface grows past a couple of endpoints, generate these from the OpenAPI schema FastAPI
 * already publishes at `/openapi.json` rather than maintaining them by hand.
 */
export interface DependencyStatus {
  readonly healthy: boolean;
  readonly detail: string | null;
}

/**
 * The guest purge, as `/health/ready` reports it (ADR-0019, slice 1.6).
 *
 * Seven fields: a flag, an instant, two counts, a word, a staleness verdict and a detail. **Nothing
 * here identifies anybody** — no session id, no filename, no storage key, no path (AC-36) — and
 * nothing here can change `ready` or the status code (AC-32); `jobs` is read, `dependencies`
 * decides.
 *
 * `detail` carries an *exception type name* when a field could not be computed, and this client
 * deliberately never renders it — see `features/health/purgeView.ts`, which does not carry it onto
 * the view at all.
 */
export interface GuestPurgeStatus {
  /** Whether a schedule fires this job at all (`GUEST_PURGE_ENABLED`). */
  readonly scheduled: boolean;
  /** RFC 3339, UTC, whole-second. `null` when it has never run, or the heartbeat was flushed. */
  readonly last_run: string | null;
  /** `null` exactly when `last_run` is. */
  readonly last_run_age_seconds: number | null;
  readonly last_outcome: 'ok' | 'failed' | null;
  /** Expired sessions still waiting. **`null` when the count could not be taken** (R-29). */
  readonly overdue: number | null;
  /** `scheduled && (last_run is null || age > 3h)`. `scheduled: false` implies `false` (AC-33). */
  readonly stale: boolean;
  /** An exception *type*, only when a field above could not be computed. Never rendered. */
  readonly detail: string | null;
}

export interface Readiness {
  readonly ready: boolean;
  readonly dependencies: Record<string, DependencyStatus>;
  /**
   * The scheduled jobs this deployment reports.
   *
   * **Both `jobs` and `guest_purge` are optional here and *required* in the API's Pydantic model,
   * and that asymmetry is deliberate rather than an oversight** (R-32). It reads as an
   * inconsistency, so: this API always sends the member, but during a deploy a browser can be
   * served a bundle *newer* than the API process answering it. The two declarations of this one
   * contract are versioned independently — one is in an image, one is on a CDN — so the client
   * types what it might actually receive, not what the current server promises. An older API with
   * no `jobs` member must render the *empty* state ("not reported by this API"), which is a
   * sentence an operator can act on, rather than throwing on a missing property.
   *
   * The server, having no such uncertainty, never omits it. Widening the *server* type to match
   * would be the wrong fix: it would make a field that is always present look optional to every
   * construction site, which is how a fact nobody computed gets published as a confident default.
   */
  readonly jobs?: {
    readonly guest_purge?: GuestPurgeStatus;
  };
}

/**
 * Fetch the readiness report.
 *
 * 503 is accepted rather than thrown: an unready API answers 503 *with* the report naming which
 * dependency is down, and that report is precisely what the caller asked for.
 */
export function fetchReadiness(signal?: AbortSignal): Promise<Readiness> {
  return request<Readiness>('/health/ready', {
    acceptStatuses: [503],
    ...(signal ? { signal } : {}),
  });
}
