import { request, requestBlob } from './client';
import { HISTORY_PAGE_SIZE } from './history';

import type { Role } from '@/features/auth/types';
import type { ExportJobListResponse, InlineExportFormat } from '@/features/export/types';
import type { HistoryPage } from '@/features/history/types';
import type { JobPosting, JobPostingListResponse } from '@/features/posting/types';
import type { SavedBaseCv, SavedBaseCvList } from '@/features/savedCvs/types';
import type { TailoredDocumentKind, TailoringRun } from '@/features/tailoring/types';

/**
 * The admin probe — `GET /api/admin/access` (slice 4.1, technical plan §4). Transport only.
 *
 * Resolves (204, no body) for an admin. Every refusal is an `ApiError`:
 * - **404** for a signed-in non-admin. The body is Starlette's `{"detail":"Not Found"}`, not the
 *   app's envelope — byte-identical to an unmatched path on purpose — so `code` is `null` and the
 *   caller branches on `status`, the one thing the firewall promises;
 * - 401 `invalid_access_token` (after `client.ts`'s one refresh-and-retry) / `not_signed_in`;
 * - 503 `service_unavailable`.
 */
export async function adminAccess(signal?: AbortSignal): Promise<void> {
  await request<null>('/api/admin/access', {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

// --- The user directory (slice 4.2, technical plan §4) --------------------------------------------
//
// Every call below is bearer-only behind the same firewall as the probe, so a non-admin's refusal
// is the same code-less 404, and a disabled actor's is 403 `account_disabled`. Every 200 is
// `no-store` on the server's side; the client keeps what it reads only in the query cache, under
// the actor's account key root, so a sign-out drops every email an admin loaded (AC-38).

/** The account directory's root. */
const USERS_PATH = '/api/admin/users';

/**
 * One row of the directory. `created_at`/`disabled_at` are ISO-8601 with an offset, whole seconds;
 * `disabled_at` is `null` for an active account.
 */
export interface AdminUserSummary {
  readonly id: string;
  readonly email: string;
  readonly role: Role;
  readonly created_at: string;
  readonly disabled_at: string | null;
}

/** One page, newest first. `next_cursor` is opaque, passed back verbatim; `null` on the last page. */
export interface UserDirectoryPage {
  readonly items: readonly AdminUserSummary[];
  readonly next_cursor: string | null;
}

/** What an account holds — the same counts `erase-account --dry-run` prints. */
export interface AccountCounts {
  readonly base_cvs: number;
  readonly files: number;
  readonly logins: number;
  readonly tailoring_runs: number;
  readonly job_postings: number;
  readonly export_jobs: number;
  readonly tracked_applications: number;
}

export interface AdminUserDetail extends AdminUserSummary {
  readonly counts: AccountCounts;
}

/** The answer to a disable or an enable. `changed` is `false` when it already was. */
export interface AccountStatusChange {
  readonly user_id: string;
  readonly disabled_at: string | null;
  readonly changed: boolean;
  readonly logins_revoked: number;
}

/** What an erase removed. `unlink_failures > 0` leaves files for the orphan sweep (OQ-11). */
export interface AccountErasureReport {
  readonly base_cvs: number;
  readonly files: number;
  readonly files_unlinked: number;
  readonly unlink_failures: number;
  readonly tailoring_runs: number;
  readonly job_postings: number;
  readonly export_jobs: number;
  readonly tracked_applications: number;
}

function userPath(userId: string): string {
  return `${USERS_PATH}/${encodeURIComponent(userId)}`;
}

/**
 * One page of the directory. `q` is the email search (`''` = everyone; sent only when non-empty),
 * `cursor` is `null` for the first page. Refusals: 422 `invalid_search` / `invalid_cursor` /
 * `validation_error`.
 */
export function listUsers(
  q: string,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<UserDirectoryPage> {
  const query = new URLSearchParams();
  if (q !== '') query.set('q', q);
  if (cursor !== null) query.set('cursor', cursor);
  const search = query.toString();
  return request<UserDirectoryPage>(search === '' ? USERS_PATH : `${USERS_PATH}?${search}`, {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/** One account and its counts. 404 `user_not_found` when it does not exist (or was erased). */
export function getUser(userId: string, signal?: AbortSignal): Promise<AdminUserDetail> {
  return request<AdminUserDetail>(userPath(userId), {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/** Disable an account and revoke its logins. Idempotent (`changed: false`). 404 `user_not_found`. */
export function disableUser(userId: string): Promise<AccountStatusChange> {
  return request<AccountStatusChange>(`${userPath(userId)}/disable`, {
    method: 'POST',
    auth: 'required',
  });
}

/** Enable an account (`logins_revoked: 0`). Idempotent. 404 `user_not_found`. */
export function enableUser(userId: string): Promise<AccountStatusChange> {
  return request<AccountStatusChange>(`${userPath(userId)}/enable`, {
    method: 'POST',
    auth: 'required',
  });
}

/** Erase an account: rows committed, then files. Irreversible. 404 `user_not_found`. */
export function eraseUser(userId: string): Promise<AccountErasureReport> {
  return request<AccountErasureReport>(userPath(userId), {
    method: 'DELETE',
    auth: 'required',
  });
}

// --- An account's content (slice 4.3, technical plan §5, ADR-0034) --------------------------------
//
// Support reads and two deletes under `/api/admin/users/{id}/…`, behind the same firewall. The
// shapes are the account's own `/api/me/` shapes — the server calls the same use cases — except one
// CV, which carries its text. Every refusal is an `ApiError`: 404 `user_not_found` (the account is
// gone), the item kind's own 404, the firewall's code-less 404, 403 `account_disabled`, 503.

/** One saved CV **with its extracted text** — `null` while not extracted. Admin only (ADR-0034). */
export interface AdminSavedBaseCv extends SavedBaseCv {
  readonly extracted_text: string | null;
}

/** A saved CV's deletion. `file_unlinked: false` leaves the bytes to the orphan sweep. */
export interface AdminBaseCvDeletion {
  readonly file_unlinked: boolean;
}

/** What deleting one history entry removed: rows committed, then files. */
export interface AdminHistoryEntryErasure {
  readonly tailoring_runs: number;
  readonly job_postings: number;
  readonly export_jobs: number;
  readonly tracked_applications: number;
  readonly files_unlinked: number;
  readonly unlink_failures: number;
}

/**
 * Whose content an admin read is about (slice 4.4, plan §5): an account or a guest session. It
 * decides the API prefix only; the server authorizes the item against it.
 */
export type AdminSubject =
  | { readonly kind: 'user'; readonly userId: string }
  | { readonly kind: 'guest'; readonly sessionId: string };

/** `/api/admin/users/{id}` or `/api/admin/guest-sessions/{id}`. */
export function subjectPath(subject: AdminSubject): string {
  return subject.kind === 'user'
    ? userPath(subject.userId)
    : `/api/admin/guest-sessions/${encodeURIComponent(subject.sessionId)}`;
}

function subjectContentPath(subject: AdminSubject, ...segments: readonly string[]): string {
  return [subjectPath(subject), ...segments.map(encodeURIComponent)].join('/');
}

function contentPath(userId: string, ...segments: readonly string[]): string {
  return subjectContentPath({ kind: 'user', userId }, ...segments);
}

function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { auth: 'required', ...(signal ? { signal } : {}) });
}

/** The account's saved CVs, newest first, without text. */
export function listUserCvs(userId: string, signal?: AbortSignal): Promise<SavedBaseCvList> {
  return read(contentPath(userId, 'base-cvs'), signal);
}

/** One saved CV with its text. 404 `base_cv_not_found`. */
export function getUserCv(
  subject: AdminSubject,
  baseCvId: string,
  signal?: AbortSignal,
): Promise<AdminSavedBaseCv> {
  return read(subjectContentPath(subject, 'base-cvs', baseCvId), signal);
}

/** Delete a saved CV: the row, then its file. Irreversible. 404 `base_cv_not_found`. */
export function deleteUserCv(userId: string, baseCvId: string): Promise<AdminBaseCvDeletion> {
  return request<AdminBaseCvDeletion>(contentPath(userId, 'base-cvs', baseCvId), {
    method: 'DELETE',
    auth: 'required',
  });
}

/** The account's 20 most recent job postings, as summaries (OQ-6: no cursor). */
export function listUserPostings(
  userId: string,
  signal?: AbortSignal,
): Promise<JobPostingListResponse> {
  return read(contentPath(userId, 'job-postings'), signal);
}

/** One job posting in full. 404 `job_posting_not_found`. */
export function getUserPosting(
  subject: AdminSubject,
  postingId: string,
  signal?: AbortSignal,
): Promise<JobPosting> {
  return read(subjectContentPath(subject, 'job-postings', postingId), signal);
}

/** One page of the account's history; `cursor` is opaque, `null` for the first page. */
export function fetchUserHistoryPage(
  userId: string,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<HistoryPage> {
  const query = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) });
  if (cursor !== null) query.set('cursor', cursor);
  return read(`${contentPath(userId, 'tailoring-runs')}?${query.toString()}`, signal);
}

/** One run with both documents. 404 `tailoring_run_not_found`. */
export function getUserRun(
  subject: AdminSubject,
  runId: string,
  signal?: AbortSignal,
): Promise<TailoringRun> {
  return read(subjectContentPath(subject, 'tailoring-runs', runId), signal);
}

/** Every export job of one run, newest first. 404 `tailoring_run_not_found`. */
export function listUserRunExports(
  subject: AdminSubject,
  runId: string,
  signal?: AbortSignal,
): Promise<ExportJobListResponse> {
  return read(subjectContentPath(subject, 'tailoring-runs', runId, 'exports'), signal);
}

/**
 * A succeeded run's document as `.md` / `.txt`, rendered in the request. 409
 * `tailoring_run_not_exportable`; 500 `render_failed`; 503 `render_timed_out`.
 */
export function downloadUserDocument(
  subject: AdminSubject,
  runId: string,
  document: TailoredDocumentKind,
  format: InlineExportFormat,
  signal?: AbortSignal,
): Promise<Blob> {
  const path = subjectContentPath(
    subject,
    'tailoring-runs',
    runId,
    'documents',
    document,
    'download',
  );
  return requestBlob(`${path}?format=${format}`, {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * An export job's stored file. 404 `export_job_not_found`; 409 `export_not_ready`; 410
 * `export_file_gone`; 503 `storage_unavailable`.
 */
export function downloadUserExportFile(
  subject: AdminSubject,
  exportJobId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  return requestBlob(subjectContentPath(subject, 'export-jobs', exportJobId, 'file'), {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Delete one history entry (the run, its exports and files, its posting if unused, its card).
 * 404 `tailoring_run_not_found`; 409 `tailoring_run_in_progress` (+ `status`) deletes nothing.
 */
export function deleteUserHistoryEntry(
  userId: string,
  runId: string,
): Promise<AdminHistoryEntryErasure> {
  return request<AdminHistoryEntryErasure>(contentPath(userId, 'tailoring-runs', runId), {
    method: 'DELETE',
    auth: 'required',
  });
}
