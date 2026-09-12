import { useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import { ApiError } from '@/api/client';

import { FetchFailureNotice } from './FetchFailureNotice';
import { JobPostingCard } from './JobPostingCard';
import { JobPostingInput } from './JobPostingInput';
import { useCreateJobPosting } from '../hooks/useCreateJobPosting';
import { useJobPostings } from '../hooks/useJobPostings';
import { latestPosting } from '../latestPosting';
import { isFetchFailureCode } from '../types';

import type { PostingSource } from '../types';

/**
 * UX-only mirror of the API's 30,000-character ceiling. It enforces nothing: the API measures the
 * real thing and is the only authority (Constitution §4.5). It exists so the counter has a
 * denominator and an obviously-too-long paste does not cost a round trip.
 *
 * Measured in the same unit the server measures it in — the length of the text as typed, which is
 * what `character_count` reports. Two units would put "35,999 / 30,000" on screen for text the
 * server just accepted.
 */
const MAX_POSTING_CHARACTERS = 30_000;

/**
 * The job-posting surface — a **container**, wired to `useJobPostings` and `useCreateJobPosting`.
 *
 * It owns which of loading / empty / submitting / success is showing, the input mode, and which of
 * the three textually-distinct error kinds applies. The markup for each piece lives in
 * `JobPostingInput`, `JobPostingCard` and `FetchFailureNotice`.
 *
 * **No `useEffect`.** There is nothing outside React to synchronize with: the postings are server
 * state and live entirely in TanStack Query. Local state is exactly four things, all transient and
 * single-owner — the input mode, the two drafts, and the client-side pre-check message.
 */
export function JobPostingPanel(): React.JSX.Element {
  const {
    data,
    isPending: listIsPending,
    isError: listIsError,
    error: listError,
  } = useJobPostings();
  const create = useCreateJobPosting();

  const [mode, setMode] = useState<PostingSource>('pasted');
  const [text, setText] = useState('');
  const [url, setUrl] = useState('');
  const [preCheckError, setPreCheckError] = useState<string | null>(null);
  const [isReplacing, setIsReplacing] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  /**
   * Error A — rejected before the request. UX only: its entire job is to avoid a round trip for
   * something the API was always going to refuse, by never issuing the request at all.
   *
   * Deliberately permissive. It rejects only what is *certainly* wrong — a blank field, a URL with
   * no http(s) scheme — and lets everything else through to the API, which owns the real rules. A
   * pre-check that duplicated the server's 100-character floor would be a second copy of a business
   * rule in TypeScript, and the copy that goes stale.
   */
  function preCheck(): string | null {
    if (mode === 'pasted') {
      return text.trim() === '' ? 'Paste the job description first.' : null;
    }
    const trimmed = url.trim();
    if (trimmed === '') {
      return 'Paste a link to the job posting first.';
    }
    if (!/^https?:\/\//i.test(trimmed)) {
      return 'That link needs to start with http:// or https://.';
    }
    return null;
  }

  function handleSubmit(): void {
    const problem = preCheck();
    if (problem !== null) {
      setPreCheckError(problem);
      return;
    }
    setPreCheckError(null);
    create.mutate(
      mode === 'pasted' ? { source: 'pasted', text } : { source: 'fetched', url: url.trim() },
      {
        onSuccess: () => {
          setIsReplacing(false);
          setText('');
          setUrl('');
        },
      },
    );
  }

  /** FR-2's fallback, performed rather than suggested: switch to paste mode and put the cursor in
   * the textarea. The URL is left in state so `FetchFailureNotice` can keep showing it. */
  function handlePasteInstead(): void {
    // `flushSync` rather than a `useEffect` or a `requestAnimationFrame`, and this is the one place
    // in the feature that reaches for it. The problem: the textarea does not exist until the mode
    // change has rendered, so `textareaRef.current` is null on the line after `setMode`.
    //
    // The three ways to solve it are not equally good. A `useEffect` keyed on `mode` would fire on
    // EVERY mode change, including a user simply clicking the radio — stealing focus from the radio
    // they just chose, which is worse than not focusing at all. `requestAnimationFrame` defers to a
    // frame that may not have run when the next line executes, so the focus is real but untestable
    // and, worse, racy against a user who starts typing.
    //
    // `flushSync` says exactly what is meant: render this state change now, synchronously, because
    // the next statement depends on the DOM it produces. React warns against it in hot paths for
    // good reason — it defeats batching — and a once-per-failed-fetch button is not a hot path.
    flushSync(() => {
      setMode('pasted');
    });
    textareaRef.current?.focus();
    // NOTE what is deliberately NOT done here: `create.reset()`. Clearing the mutation error would
    // unmount `FetchFailureNotice` — and the notice is where the URL is still on screen. Taking it
    // away at the exact moment the user needs to open it in a tab and copy from it would undo the
    // point of the button. The notice clears itself on the next successful submit.
  }

  // loading — no input control yet. An interactive control that is about to be replaced reads to a
  // user as a flicker, not as loading.
  if (listIsPending) {
    return (
      <p role="status" className="text-sm text-slate-500">
        Loading your job posting…
      </p>
    );
  }

  if (listIsError) {
    // The list itself failing to load is a different fact from anything about a specific posting,
    // so it gets its own message rather than being folded into one of the three error kinds.
    return (
      <p role="alert" className="text-sm text-red-700">
        Could not load your job postings: {listError.message}
      </p>
    );
  }

  const items = data.items;
  const latest = latestPosting(items);
  const showCard = latest !== null && !isReplacing && !create.isPending;

  // Error C is a fetch failure; error B is everything else the API refused. Only one of the two is
  // fixed by changing the input, which is why they are told apart here rather than rendered alike.
  const apiError = create.error instanceof ApiError ? create.error : null;
  const isFetchFailure = apiError !== null && isFetchFailureCode(apiError.code);

  return (
    <div className="space-y-3">
      {showCard ? (
        <JobPostingCard
          posting={latest}
          onReplace={() => {
            setIsReplacing(true);
          }}
        />
      ) : (
        <>
          <JobPostingInput
            mode={mode}
            text={text}
            url={url}
            disabled={create.isPending}
            maxCharacters={MAX_POSTING_CHARACTERS}
            textareaRef={textareaRef}
            onModeChange={(next) => {
              setMode(next);
              setPreCheckError(null);
            }}
            onTextChange={setText}
            onUrlChange={setUrl}
          />

          <button
            type="button"
            onClick={handleSubmit}
            disabled={create.isPending}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
          >
            {/* Two different verbs, because only one of them is waiting on someone else's server.
                A user told "Saving…" for eight seconds concludes we are broken; "Reading the job
                posting…" says where the time is going and that it is not our fault. */}
            {create.isPending
              ? mode === 'fetched'
                ? 'Reading the job posting…'
                : 'Saving…'
              : 'Add job posting'}
          </button>
        </>
      )}

      {items.length === 0 && !create.isPending && (
        // AC-23: the retention promise, stated before the user commits anything (ADR-0006 §5).
        <p className="text-sm text-slate-500">We delete guest job postings after 24 hours.</p>
      )}

      {preCheckError !== null && (
        // Error A. Inline text, not a boxed notice: nothing was sent, so this reads closer to form
        // validation than to an incident.
        <p role="alert" className="text-sm font-medium text-red-700">
          {preCheckError}
        </p>
      )}

      {create.isError && isFetchFailure && (
        // Error C — the link could not be read.
        <FetchFailureNotice
          message={create.error.message}
          url={url}
          onPasteInstead={handlePasteInstead}
        />
      )}

      {create.isError && !isFetchFailure && (
        // Error B — the API refused the request itself.
        <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2">
          <p className="text-sm font-medium text-red-700">{create.error.message}</p>
        </div>
      )}
    </div>
  );
}
