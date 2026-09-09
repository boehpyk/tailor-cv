import { useState } from 'react';

import { ApiError } from '@/api/client';

import { BaseCvCard } from './BaseCvCard';
import { CvDropzone } from './CvDropzone';
import { UploadErrorNotice } from './UploadErrorNotice';
import { useBaseCvs } from '../hooks/useBaseCvs';
import { useUploadBaseCv } from '../hooks/useUploadBaseCv';

import type { BaseCv } from '../types';

/** UX-only mirror of the API's `max_upload_bytes` (10 MB). Saves a user a round trip for an
 * obviously-too-big file; it enforces nothing — the API measures the real bytes as they stream in
 * and is the only authority (Constitution §4.5). */
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

/** UX-only mirror of the three formats the API accepts, checked by extension. The API decides the
 * real answer by sniffing the file's bytes (`sniff_cv_content_type`), never the extension or the
 * browser-supplied `Content-Type` — so a file that passes this check can still come back 415, and
 * a file that fails it was never going to be given the chance to find out. */
const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.txt'] as const;

function hasAcceptedExtension(filename: string): boolean {
  const lower = filename.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

/**
 * Client-side pre-check — **error A**, "rejected before upload".
 *
 * This is UX only, not validation: its entire job is to save a user a 10 MB round trip for a file
 * the API was always going to refuse, by never issuing the request at all. The API re-validates
 * the extension, the size (while streaming, not after buffering the whole file) and the actual
 * content on every upload; nothing here is a substitute for that.
 */
function preValidate(file: File): string | null {
  if (!hasAcceptedExtension(file.name)) {
    return 'Please choose a PDF, DOCX or TXT file.';
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return 'That file is larger than 10 MB. Please choose a smaller file.';
  }
  return null;
}

/** The most recently uploaded `BaseCv`, or `null` for a session with none. Compared by
 * `uploaded_at` rather than array position — the list endpoint's ordering is not part of its
 * contract (technical-plan.md's API contract section says nothing about order), so reading
 * `items[0]` as "latest" would be trusting a guarantee the server never made. */
function latestBaseCv(items: readonly BaseCv[]): BaseCv | null {
  if (items.length === 0) {
    return null;
  }
  return items.reduce((latest, item) => (item.uploaded_at > latest.uploaded_at ? item : latest));
}

/**
 * The base-CV upload surface — a **container**, wired to `useBaseCvs` (the list) and
 * `useUploadBaseCv` (the mutation). It owns which of loading / empty / uploading / success is
 * showing, and which of the three textually-distinct error kinds (AC-15); the actual markup for
 * each piece lives in `CvDropzone`, `BaseCvCard` and `UploadErrorNotice` (T35).
 *
 * **No `useEffect`.** There is nothing outside React to synchronize with here — the CV list is
 * server state and lives entirely in TanStack Query; the only local state is transient rendering
 * detail that belongs to this component alone: the client-side pre-check message, and whether the
 * user has asked to replace an already-successful CV.
 */
export function BaseCvUploadPanel(): React.JSX.Element {
  const { data, isPending: listIsPending, isError: listIsError, error: listError } = useBaseCvs();
  const upload = useUploadBaseCv();
  const [preValidationError, setPreValidationError] = useState<string | null>(null);
  // "Replace CV" (BaseCvCard) swaps the success card back out for the dropzone. It resets itself
  // once a replacement upload succeeds (below) — there is no case where a stale `true` should
  // survive past that, so there is nothing here for a `useEffect` to synchronize.
  const [isReplacing, setIsReplacing] = useState(false);

  function handleFileChosen(file: File): void {
    const problem = preValidate(file);
    if (problem !== null) {
      // Error A. No request is issued — that is the entire point of a client-side pre-check.
      setPreValidationError(problem);
      return;
    }
    setPreValidationError(null);
    upload.mutate(file, {
      onSuccess: () => {
        setIsReplacing(false);
      },
    });
  }

  // loading — the list query hasn't settled. Deliberately no dropzone here: an interactive control
  // that is about to be swapped out reads to a user as a flicker, not as loading (technical-plan.md).
  if (listIsPending) {
    return (
      <div>
        <p role="status" className="text-sm text-slate-500">
          Loading your CV…
        </p>
      </div>
    );
  }

  if (listIsError) {
    // Not one of AC-15's four upload states — this is the list itself failing to load, the
    // SystemStatus.tsx pattern applied here: "could not reach the API" is a different fact from
    // anything about a specific file, so it gets its own message rather than being folded into one
    // of the upload-specific error kinds.
    return (
      <div>
        <p role="alert" className="text-sm text-red-700">
          Could not load your CVs: {listError.message}
        </p>
      </div>
    );
  }

  const items = data.items;
  const isEmpty = items.length === 0;
  const latest = latestBaseCv(items);
  const isSuccess = latest?.status === 'extracted';
  const showCard = isSuccess && !isReplacing && !upload.isPending;

  return (
    <div className="space-y-3">
      {showCard ? (
        <BaseCvCard
          cv={latest}
          onReplace={() => {
            setIsReplacing(true);
          }}
        />
      ) : (
        <CvDropzone
          disabled={upload.isPending}
          // The chosen filename comes from the mutation's own `variables`, not a second piece of
          // local state that would just be a copy of it.
          uploadingFileName={upload.isPending ? upload.variables.name : null}
          onFileChosen={handleFileChosen}
        />
      )}

      {isEmpty && !upload.isPending && (
        <p className="text-sm text-slate-500">We delete guest CVs after 24 hours.</p>
      )}

      {preValidationError !== null && (
        <UploadErrorNotice kind="preValidation" message={preValidationError} />
      )}

      {upload.isError && (
        // Error B — rejected by the API (413 / 415 / 422 / 409 / 429). `upload.error` is typed as
        // `Error` (useUploadBaseCv.ts) because a transport-level failure (offline, DNS) throws a
        // plain `Error`, not an `ApiError` — the `instanceof` guard is what lets this stay honest
        // for that case instead of inventing a status/code that was never on the wire.
        <UploadErrorNotice
          kind="apiError"
          message={upload.error.message}
          status={upload.error instanceof ApiError ? upload.error.status : 0}
          code={upload.error instanceof ApiError ? upload.error.code : null}
        />
      )}

      {latest?.status === 'extraction_failed' && (
        // Error C — stored but unreadable (201 with `status: "extraction_failed"`). Must be
        // textually distinct from A and B (AC-15): this is "the file is unreadable", not "the
        // upload broke", and only one of those is fixed by retrying. `failure_message` is
        // server-owned (technical-plan.md's API contract) so the client renders it rather than
        // re-deciding the wording per `failure_reason`; the literal fallback only guards the type
        // (`failure_message: string | null`) for a case the domain should never actually produce.
        <UploadErrorNotice
          kind="extractionFailed"
          message={
            latest.failure_message ??
            "We saved your file but couldn't read any text from it — it looks like a scan. Try a text-based PDF, or paste your CV as a .txt file."
          }
        />
      )}
    </div>
  );
}
