/**
 * **Which API a workspace talks to** — the paths and the credential that differ between a guest's
 * workspace and a signed-in user's (slice 2.3, technical plan §0.9). Transport only: no query key,
 * no link, nothing React knows about. `features/scope/scopeMap.ts` builds one of these per scope and
 * adds the client-side half (keys, links) on top.
 *
 * The account routes are **twins** of the guest ones under `/api/me/` (plan §0.2): the same bodies,
 * the same response shapes, a different owner and a different credential. So a function in
 * `api/tailoringRuns.ts` or `api/exports.ts` takes a target rather than being written twice — two
 * copies of one call is how 2.2's R-6 drift starts, in TypeScript this time.
 *
 * **The path and the credential travel together, on purpose.** A guest path sent with the bearer is
 * the first step of 2.4's claim happening without anyone designing it; an account path sent without
 * one is a 401 at best. One value holding both means a caller cannot pick one of each (AC-48).
 */
export interface ApiTarget {
  /** The run collection: `POST` to create, `/{id}` to read, `/{id}/documents/…`, `/{id}/exports`. */
  readonly tailoringRunsPath: string;
  /** The flat export-job resource: `/{id}` to poll, `/{id}/file` for the bytes. */
  readonly exportJobsPath: string;
  /** The posting collection. */
  readonly jobPostingsPath: string;
  /**
   * `'required'` for account data — every request carries the bearer; `null` for a guest's — no
   * request carries one, and the `tc_guest` cookie authorizes it.
   */
  readonly auth: 'required' | null;
}

/** Today's guest routes, exactly as 1.2–1.5 wrote them. */
export const GUEST_API_TARGET: ApiTarget = {
  tailoringRunsPath: '/api/tailoring-runs',
  exportJobsPath: '/api/export-jobs',
  jobPostingsPath: '/api/job-postings',
  auth: null,
};

/**
 * The account's twins (plan §4). Independent of *which* user: the bearer says who, and the paths
 * are the same for everyone — the user id is in the query keys, never in a URL.
 */
export const ACCOUNT_API_TARGET: ApiTarget = {
  tailoringRunsPath: '/api/me/tailoring-runs',
  exportJobsPath: '/api/me/export-jobs',
  jobPostingsPath: '/api/me/job-postings',
  auth: 'required',
};

/**
 * The `auth` field of `request`/`requestBlob`'s options for a target — spread into them, because
 * under `exactOptionalPropertyTypes` an optional option may be absent but not `undefined`, and a
 * guest request must carry no `auth` key at all.
 */
export function authOptionFor(target: ApiTarget): { readonly auth?: 'required' } {
  return target.auth === null ? {} : { auth: target.auth };
}
