import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';

import { activeTailoringRunId, rejectionMessage, runReadErrorCopy } from './apiErrorCopy';

/**
 * T44 — `apiErrorCopy.ts` maps from `code`, **never** `message` (task-list T44's own words).
 * `TailorPanel.test.tsx`'s error-B tests already prove this for three codes, incidentally, while
 * exercising the whole panel; this file proves it directly, for every code the module's docblock
 * says it handles, by holding `code` fixed and swapping `message` — the one lever the server
 * controls that must never move the UI's copy.
 */

const JUNK_A = 'ZZZZ_SERVER_MESSAGE_A_NOT_FOR_USERS';
const JUNK_B = 'a completely different server message, in case parts of it leak through';

describe('rejectionMessage', () => {
  const KNOWN_CODES = [
    'validation_error',
    'request_too_large',
    'guest_session_expired',
    'base_cv_not_found',
    'job_posting_not_found',
    'base_cv_not_extracted',
    'tailoring_already_running',
    'too_many_tailoring_runs',
    'rate_limited',
    'rate_limit_unavailable',
    'queue_unavailable',
    'service_unavailable',
  ] as const;

  it.each(KNOWN_CODES)(
    'is driven by code alone for %s — two different server messages give the same copy',
    (code) => {
      const a = rejectionMessage(new ApiError(409, JUNK_A, code));
      const b = rejectionMessage(new ApiError(409, JUNK_B, code));

      expect(a).toBe(b);
      expect(a).not.toBe(JUNK_A);
      expect(a).not.toBe(JUNK_B);
    },
  );

  it('never relays the server message even for a code this module does not recognise', () => {
    const a = rejectionMessage(new ApiError(418, JUNK_A, 'brand_new_code_not_yet_mapped'));
    const b = rejectionMessage(new ApiError(418, JUNK_B, 'brand_new_code_not_yet_mapped'));

    expect(a).toBe(b);
    expect(a).not.toBe(JUNK_A);
  });

  it('still distinguishes a 5xx from a 4xx for an unrecognised code, by status rather than message', () => {
    const serverError = rejectionMessage(new ApiError(503, JUNK_A, 'brand_new_code'));
    const clientError = rejectionMessage(new ApiError(418, JUNK_A, 'brand_new_code'));

    expect(serverError).not.toBe(clientError);
  });

  it('reports a transport-level failure (no envelope, so no ApiError at all) without touching its message', () => {
    const message = rejectionMessage(new Error(JUNK_A));

    expect(message).not.toBe(JUNK_A);
    expect(message.length).toBeGreaterThan(0);
  });
});

describe('runReadErrorCopy', () => {
  const KNOWN_4XX_CODES = ['tailoring_run_not_found', 'guest_session_expired'] as const;

  it.each(KNOWN_4XX_CODES)('is driven by code alone for %s', (code) => {
    const a = runReadErrorCopy(new ApiError(404, JUNK_A, code));
    const b = runReadErrorCopy(new ApiError(404, JUNK_B, code));

    expect(a).toEqual(b);
    expect(a.message).not.toBe(JUNK_A);
  });

  it('offers no "check again" for any 4xx — known code or not', () => {
    const known = runReadErrorCopy(new ApiError(404, JUNK_A, 'tailoring_run_not_found'));
    const unrecognised = runReadErrorCopy(new ApiError(422, JUNK_A, 'validation_error'));

    expect(known.canCheckAgain).toBe(false);
    expect(unrecognised.canCheckAgain).toBe(false);
  });

  it('treats a 5xx as transient and worth a free re-check, regardless of message', () => {
    const a = runReadErrorCopy(new ApiError(503, JUNK_A, 'service_unavailable'));
    const b = runReadErrorCopy(new ApiError(503, JUNK_B, 'service_unavailable'));

    expect(a).toEqual(b);
    expect(a.canCheckAgain).toBe(true);
  });

  it('treats a network failure (no ApiError) the same as a transient 5xx', () => {
    const copy = runReadErrorCopy(new Error(JUNK_A));

    expect(copy.canCheckAgain).toBe(true);
    expect(copy.message).not.toBe(JUNK_A);
  });
});

describe('activeTailoringRunId', () => {
  it('reads the id from details, never from the message', () => {
    const error = new ApiError(409, JUNK_A, 'tailoring_already_running', {
      active_tailoring_run_id: 'run-123',
    });

    expect(activeTailoringRunId(error)).toBe('run-123');
  });

  it('returns null when the code is not tailoring_already_running, even if details carries the key', () => {
    const error = new ApiError(409, JUNK_A, 'too_many_tailoring_runs', {
      active_tailoring_run_id: 'run-123',
    });

    expect(activeTailoringRunId(error)).toBeNull();
  });

  it('returns null when a 409 tailoring_already_running names no active run', () => {
    const error = new ApiError(409, JUNK_A, 'tailoring_already_running');

    expect(activeTailoringRunId(error)).toBeNull();
  });

  it('returns null for a non-ApiError', () => {
    expect(activeTailoringRunId(new Error(JUNK_A))).toBeNull();
  });
});
