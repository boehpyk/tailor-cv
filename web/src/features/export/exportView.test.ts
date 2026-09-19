import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';

import { makeExportJob, makeExportMutations } from './test/fixtures';
import { viewOfExport } from './exportView';

import type { ExportMutations, ExportTarget } from './exportView';

/**
 * F5 RED — `viewOfExport`'s own table (feature-spec AC-37; technical-plan "The per-format state
 * machine (derived, not stored)").
 *
 * Written against the **spec**, not `exportView.ts`'s F3 skeleton, which returns `{ kind: 'idle' }`
 * unconditionally and reads none of its four parameters (see the skeleton's own docstring, which
 * names exactly this trap: four earlier slices — 1.1's T33/T34, 1.3's T40, 1.4's F5/F8 — shipped a
 * frontend shell that already worked, so the tests written against it passed on arrival and were
 * never observed failing). Every row below fails on a real mismatch of the returned `ExportView` —
 * `{ kind: 'idle' }` where `{ kind: 'queued', elapsedSeconds: 7 }` was expected — never on an
 * `ImportError`, because `viewOfExport`'s signature and every type it touches already exist (F3,
 * F4).
 *
 * **One row is expected to pass on arrival, and it is not a discrimination failure**: see "no job
 * at all" below.
 */

const PDF_TARGET: ExportTarget = { document: 'cv', format: 'pdf' };
const MD_TARGET: ExportTarget = { document: 'cv', format: 'md' };

const NOW_MS = Date.parse('2026-09-18T10:00:03.000Z');

describe('viewOfExport — AC-37', () => {
  it('no job at all is idle (passes on arrival — see the note below)', () => {
    // This is the one row expected to be green the moment this test lands, and it is not
    // undiscriminating: `viewOfExport` today returns `{ kind: 'idle' }` for *every* input, so this
    // is simply the one input for which that constant happens to be the right answer too. Every
    // other row in this file supplies a job, a mutation, or both, and every one of them turns red
    // against the same stub — which is what proves the stub is being read at all, not merely that
    // this one assertion is weak.
    const view = viewOfExport(PDF_TARGET, [], makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'idle' });
  });

  it('a request in flight for this target is requesting, even with jobs present — and one for a DIFFERENT target is not', () => {
    const jobs = [makeExportJob({ document: 'cv', format: 'pdf', status: 'ready', current: true })];

    const forThisTarget = viewOfExport(
      PDF_TARGET,
      jobs,
      makeExportMutations({ requesting: PDF_TARGET }),
      NOW_MS,
    );
    expect(forThisTarget).toEqual({ kind: 'requesting' });

    // Same function, same call, only the requested target differs — proving isolation requires
    // the positive case above too: taken alone, this row would pass against a stub that always
    // answers `idle` regardless of its arguments, which is exactly the trap this file exists to
    // avoid (see the "no job at all" row's note).
    const forOtherTarget = viewOfExport(
      PDF_TARGET,
      jobs,
      makeExportMutations({ requesting: { document: 'cv', format: 'docx' } }),
      NOW_MS,
    );
    expect(forOtherTarget).toEqual({ kind: 'ready', jobId: 'export-job-fixture', byteSize: null });
  });

  it('the latest job queued is "queued", with elapsed seconds from requested_at', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'queued',
        requested_at: '2026-09-18T09:59:56.000Z', // 7s before NOW_MS
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'queued', elapsedSeconds: 7 });
  });

  it('the latest job rendering is "rendering", with elapsed seconds from started_at (the spec\'s own "3s" example)', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'rendering',
        requested_at: '2026-09-18T10:00:00.000Z',
        started_at: '2026-09-18T10:00:00.000Z', // 3s before NOW_MS
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'rendering', elapsedSeconds: 3 });
  });

  it('ready and current is "ready", carrying the job id and byte size', () => {
    const jobs = [
      makeExportJob({
        id: 'job-ready-1',
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: true,
        byte_size: 86016, // exactly 84 KiB — the spec's own "84 KB" example
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'ready', jobId: 'job-ready-1', byteSize: 86016 });
  });

  it("ready with a null byte_size stays ready — the wire's nullability, not a client-invented default", () => {
    const jobs = [
      makeExportJob({
        id: 'job-ready-null-size',
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: true,
        byte_size: null,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'ready', jobId: 'job-ready-null-size', byteSize: null });
  });

  it('ready but NOT current is "stale", carrying the job id', () => {
    const jobs = [
      makeExportJob({
        id: 'job-stale-1',
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: false,
        byte_size: 12345,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'stale', jobId: 'job-stale-1' });
  });

  it("failed carries the server's reason and retryable, untouched", () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'failed',
        failure_reason: 'render_timed_out',
        retryable: true,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'failed', reason: 'render_timed_out', retryable: true });
  });

  it('failed and NOT retryable carries retryable: false, unchanged', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'failed',
        failure_reason: 'render_failed',
        retryable: false,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'failed', reason: 'render_failed', retryable: false });
  });

  it('failed with a null reason stays failed — one schema serves all four statuses, so this is reachable on the wire', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'failed',
        failure_reason: null,
        retryable: false,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'failed', reason: null, retryable: false });
  });

  it('a download in flight for this target is "downloading", even over a ready job', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: true,
        byte_size: 1,
      }),
    ];
    const mutations = makeExportMutations({ downloading: PDF_TARGET });

    const view = viewOfExport(PDF_TARGET, jobs, mutations, NOW_MS);

    expect(view).toEqual({ kind: 'downloading' });
  });

  it('a rejected download for this target is "downloadFailed", over a ready job, mapped from the ApiError — nextAction is "download" for a 5xx (the file may still be there)', () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: true,
        byte_size: 1,
      }),
    ];
    const mutations = makeExportMutations({
      downloadFailure: {
        target: PDF_TARGET,
        error: new ApiError(500, 'boom', null),
      },
    });

    const view = viewOfExport(PDF_TARGET, jobs, mutations, NOW_MS);

    // A single `toEqual` on the whole object, not a `.kind` narrow followed by a property read:
    // `nextAction` does not exist on `ExportView['downloadFailed']` yet (that is MAJOR 2's whole
    // point), and reading a field that is not on the type would be a compile error, not a runtime
    // red — exactly the kind of false failure the RED tier's own rules rule out.
    //
    // MAJOR 2 (/verify slice 1.5): a 5xx says nothing about whether the file still exists, so the
    // retry must repeat the same download rather than paying for a render nobody asked for.
    expect(view).toEqual({
      kind: 'downloadFailed',
      message: "Couldn't download",
      nextAction: 'download',
    });
  });

  it('a network failure (not an ApiError) is downloadFailed with nextAction "download"', () => {
    const mutations = makeExportMutations({
      downloadFailure: {
        target: PDF_TARGET,
        error: new TypeError('Failed to fetch'),
      },
    });

    const view = viewOfExport(PDF_TARGET, [], mutations, NOW_MS);

    expect(view).toEqual({
      kind: 'downloadFailed',
      message: "Couldn't download",
      nextAction: 'download',
    });
  });

  it('a 404 export_job_not_found download failure maps to "We couldn\'t find that file" — nextAction is "request", because the job itself is gone and only a new export can help', () => {
    const mutations = makeExportMutations({
      downloadFailure: {
        target: PDF_TARGET,
        error: new ApiError(404, 'not found', 'export_job_not_found'),
      },
    });

    const view = viewOfExport(PDF_TARGET, [], mutations, NOW_MS);

    expect(view).toEqual({
      kind: 'downloadFailed',
      message: "We couldn't find that file",
      nextAction: 'request',
    });
  });

  it('a 410 export_file_gone download failure maps to "That file is no longer available — Export again" — nextAction is "request", the MAJOR 2 fix (/verify slice 1.5): the row still says ready, but the file is gone, so repeating the same GET can only 410 again', () => {
    const mutations = makeExportMutations({
      downloadFailure: {
        target: PDF_TARGET,
        error: new ApiError(410, 'gone', 'export_file_gone'),
      },
    });

    const view = viewOfExport(PDF_TARGET, [], mutations, NOW_MS);

    expect(view).toEqual({
      kind: 'downloadFailed',
      message: 'That file is no longer available — Export again',
      nextAction: 'request',
    });
  });

  it("a 409 export_not_ready download failure is NOT downloadFailed — it returns to the job's polled state", () => {
    const jobs = [
      makeExportJob({
        document: 'cv',
        format: 'pdf',
        status: 'rendering',
        requested_at: '2026-09-18T10:00:00.000Z',
        started_at: '2026-09-18T10:00:00.000Z',
      }),
    ];
    const mutations = makeExportMutations({
      downloadFailure: {
        target: PDF_TARGET,
        error: new ApiError(409, 'not ready', 'export_not_ready'),
      },
    });

    const view = viewOfExport(PDF_TARGET, jobs, mutations, NOW_MS);

    expect(view).toEqual({ kind: 'rendering', elapsedSeconds: 3 });
  });

  it('picks the LATEST job when several exist for the same target (list order is newest-first), and ignores jobs of a different document or format entirely', () => {
    const jobs = [
      // Neither of these matches (document, format) — present to prove they are skipped, not
      // merely absent; taken without the two matching jobs below, this row alone would pass
      // against a stub that always answers `idle`.
      makeExportJob({ document: 'cover_letter', format: 'pdf', status: 'ready', current: true }),
      makeExportJob({ document: 'cv', format: 'docx', status: 'ready', current: true }),
      makeExportJob({
        id: 'job-newest',
        document: 'cv',
        format: 'pdf',
        status: 'ready',
        current: true,
        byte_size: 999,
      }),
      makeExportJob({
        id: 'job-oldest',
        document: 'cv',
        format: 'pdf',
        status: 'failed',
        failure_reason: 'render_error',
        retryable: true,
      }),
    ];

    const view = viewOfExport(PDF_TARGET, jobs, makeExportMutations(), NOW_MS);

    expect(view).toEqual({ kind: 'ready', jobId: 'job-newest', byteSize: 999 });
  });

  describe('an inline format (md/txt) has only the three-state machine, since there is no job', () => {
    it('idle by default, ignoring a queued job of a different format for the same document — and downloading when a download is in flight', () => {
      const jobs = [
        makeExportJob({ document: 'cv', format: 'pdf', status: 'ready', current: true }),
      ];

      // Taken alone this would pass against a stub that always answers `idle`; the `downloading`
      // assertion right after is what makes the pair discriminate.
      const idleView = viewOfExport(MD_TARGET, jobs, makeExportMutations(), NOW_MS);
      expect(idleView).toEqual({ kind: 'idle' });

      const downloadingView = viewOfExport(
        MD_TARGET,
        jobs,
        makeExportMutations({ downloading: MD_TARGET }),
        NOW_MS,
      );
      expect(downloadingView).toEqual({ kind: 'downloading' });
    });

    it('a rejected inline download is downloadFailed, mapped the same way as a queued one — nextAction is "download" for a 401 (there is no job to abandon, only a session to prove again)', () => {
      const mutations = makeExportMutations({
        downloadFailure: {
          target: MD_TARGET,
          error: new ApiError(401, 'expired', 'guest_session_expired'),
        },
      });

      const view = viewOfExport(MD_TARGET, [], mutations, NOW_MS);

      if (view.kind !== 'downloadFailed') {
        throw new Error(`expected downloadFailed, got ${view.kind}`);
      }
      expect(view.message).toMatch(/session has expired/i);
      // A second, whole-object assertion for `nextAction` rather than `view.nextAction` after the
      // narrow above: the field does not exist on `ExportView['downloadFailed']` yet (MAJOR 2,
      // `/verify` slice 1.5), and reading it here would be a compile error, not a runtime red.
      expect(view).toEqual({
        kind: 'downloadFailed',
        message: 'Your session has expired',
        nextAction: 'download',
      });
    });
  });

  describe('requestFailed — MAJOR 1 (/verify slice 1.5, iteration 2): a rejected POST /exports must reach the user', () => {
    /**
     * At RED this carried an `as ExportMutations` cast, because `ExportMutations` had no
     * `requestFailure` member — that absence *was* the finding — and without the cast this helper
     * would have been an excess-property error, i.e. a `tsc -b` failure rather than a test that reds
     * on an assertion. It was the write-side counterpart to the whole-object `toEqual` the MAJOR 2
     * tests use on the read side, for the same reason: a test written against a type that does not
     * exist yet has to survive its own compile step to be able to fail honestly.
     *
     * **The cast is gone now, and its removal is the point rather than tidying.** The field exists,
     * so the object type-checks on its own and every future edit to `ExportMutations` is checked
     * here — which a cast would have gone on silently swallowing. A RED-phase escape hatch that
     * outlives its RED is indistinguishable from a suppressed error.
     */
    function withRequestFailure(target: ExportTarget, error: Error): ExportMutations {
      return {
        ...makeExportMutations(),
        requestFailure: { target, error },
      };
    }

    // Every "User sees" cell from the five failure-contract rows the reviewer found undelivered,
    // copied verbatim from feature-spec.md (X-14, X-18, X-19, X-21, X-22) — never imported from
    // `exportCopy.ts`, for the same reason every other row in this file is copied by hand.
    const REQUEST_FAILURE_CASES: ReadonlyArray<{
      readonly name: string;
      readonly code: string;
      readonly message: string;
      readonly retryable: boolean;
    }> = [
      {
        name: 'X-14 tailoring_run_not_exportable — the run has no current documents',
        code: 'tailoring_run_not_exportable',
        message: 'This run has no documents to download yet.',
        retryable: false,
      },
      {
        name: 'X-18 too_many_export_jobs — the session already owns the cap',
        code: 'too_many_export_jobs',
        message: "You've reached the download limit for this session.",
        retryable: false,
      },
      {
        name: 'X-19 rate_limited — 30/h/session or 60/h/IP',
        code: 'rate_limited',
        message: 'Too many exports — try again in a few minutes.',
        retryable: true,
      },
      {
        name: 'X-21 service_unavailable — Postgres down, or the commit failed before the enqueue',
        code: 'service_unavailable',
        message: 'Something went wrong. Try again.',
        retryable: true,
      },
      {
        name: 'X-22 queue_unavailable — the enqueue failed after the row was committed',
        code: 'queue_unavailable',
        message: "We couldn't start preparing your file. Try again.",
        retryable: true,
      },
    ];

    it.each(REQUEST_FAILURE_CASES)('$name', ({ code, message, retryable }) => {
      const mutations = withRequestFailure(PDF_TARGET, new ApiError(409, 'server prose', code));

      const view = viewOfExport(PDF_TARGET, [], mutations, NOW_MS);

      expect(view).toEqual({ kind: 'requestFailed', message, retryable });
    });

    it('the five sentences are mutually distinct — AC-38: a user who cannot tell two states apart clicks again', () => {
      const messages = REQUEST_FAILURE_CASES.map((c) => c.message);

      expect(new Set(messages).size).toBe(messages.length);
    });

    it('a request failure for a DIFFERENT target does not surface on this one', () => {
      const mutations = withRequestFailure(
        { document: 'cv', format: 'docx' },
        new ApiError(409, 'server prose', 'tailoring_run_not_exportable'),
      );

      const view = viewOfExport(PDF_TARGET, [], mutations, NOW_MS);

      expect(view).toEqual({ kind: 'idle' });
    });

    it("a request failure outranks a stale, already-failed job for the same target — the browser's own last request is fresher than what the poll last said", () => {
      const jobs = [
        makeExportJob({
          document: 'cv',
          format: 'pdf',
          status: 'failed',
          failure_reason: 'render_error',
          retryable: true,
        }),
      ];
      const mutations = withRequestFailure(
        PDF_TARGET,
        new ApiError(429, 'server prose', 'rate_limited'),
      );

      const view = viewOfExport(PDF_TARGET, jobs, mutations, NOW_MS);

      expect(view).toEqual({
        kind: 'requestFailed',
        message: 'Too many exports — try again in a few minutes.',
        retryable: true,
      });
    });
  });
});
