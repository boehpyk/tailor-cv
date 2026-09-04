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

export interface Readiness {
  readonly ready: boolean;
  readonly dependencies: Record<string, DependencyStatus>;
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
