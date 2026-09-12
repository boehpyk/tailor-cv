import { useId } from 'react';

import type { BaseCvCheck, JobPostingCheck } from '../launchReadiness';

/** AC-25, verbatim. Stated before the first click, not buried (Constitution §8). */
const GEMINI_DISCLOSURE =
  'Tailoring sends your CV text and this job posting to Google Gemini. Nothing else is sent.';

/**
 * The retention promise, **copied verbatim** from `BaseCvUploadPanel` rather than reworded for this
 * panel (AC-25: "the retention promise from 1.1/1.2 is repeated"). The CV is the thing this block
 * is about to send, so it is the CV's sentence that belongs next to the disclosure.
 */
const RETENTION_SENTENCE = 'We delete guest CVs after 24 hours.';

export interface TailorLaunchProps {
  readonly baseCv: BaseCvCheck;
  readonly jobPosting: JobPostingCheck;
  /** Whether the session already has a run — only the button's verb depends on it. */
  readonly hasPreviousRun: boolean;
  /** The create mutation is in flight. The control is disabled so a double click cannot pay twice. */
  readonly isStarting: boolean;
  readonly onLaunch: () => void;
}

/** Why the button is disabled, in words — or `null` when both inputs are ready. */
function blockedReason(baseCv: BaseCvCheck, jobPosting: JobPostingCheck): string | null {
  const reasons: string[] = [];
  switch (baseCv.state) {
    case 'ready':
      break;
    case 'missing':
      reasons.push('Add your base CV above.');
      break;
    case 'reading':
      reasons.push("We're still reading your CV.");
      break;
    case 'unreadable':
      reasons.push("We couldn't read your CV — upload a different file above.");
      break;
  }
  if (jobPosting.state === 'missing') {
    reasons.push('Add the job posting above.');
  }
  return reasons.length === 0 ? null : reasons.join(' ');
}

function baseCvDetail(baseCv: BaseCvCheck): string {
  switch (baseCv.state) {
    case 'ready':
      return baseCv.baseCv.original_filename;
    case 'reading':
      return `${baseCv.baseCv.original_filename} (still being read)`;
    case 'unreadable':
      return `${baseCv.baseCv.original_filename} (could not be read)`;
    case 'missing':
      return 'add one above';
  }
}

function jobPostingDetail(jobPosting: JobPostingCheck): string {
  return jobPosting.state === 'ready'
    ? (jobPosting.jobPosting.title ?? 'untitled posting')
    : 'add one above';
}

function ChecklistItem({
  done,
  label,
  detail,
}: {
  readonly done: boolean;
  readonly label: string;
  readonly detail: string;
}): React.JSX.Element {
  return (
    <li className="flex gap-2">
      {/* The glyph is decoration; the detail text already says whether the item is done, so a
          screen reader is not read "check mark" or "white circle" on top of it. */}
      <span aria-hidden="true" className={done ? 'text-emerald-600' : 'text-slate-400'}>
        {done ? '✓' : '○'}
      </span>
      <span className="text-slate-800">
        {label} <span className="text-slate-500">— {detail}</span>
      </span>
    </li>
  );
}

/**
 * The launch control — presentational. The two prerequisites as a checklist, the disclosure, the
 * retention promise, and the button.
 *
 * **The disabled reason is an accessible description, not just greyed-out styling.** A disabled
 * button tells a sighted user "not now" and a screen-reader user nothing at all; `aria-describedby`
 * pointing at the reason is what makes "why" part of the control. When the button is enabled it
 * points at the Gemini disclosure instead, so the sentence AC-25 requires *before the first click*
 * is also what a screen reader reads out *on* the button.
 */
export function TailorLaunch({
  baseCv,
  jobPosting,
  hasPreviousRun,
  isStarting,
  onLaunch,
}: TailorLaunchProps): React.JSX.Element {
  const reasonId = useId();
  const disclosureId = useId();
  const reason = blockedReason(baseCv, jobPosting);

  return (
    <div className="space-y-3">
      <ul aria-label="What tailoring needs" className="space-y-1 text-sm">
        <ChecklistItem
          done={baseCv.state === 'ready'}
          label="Base CV"
          detail={baseCvDetail(baseCv)}
        />
        <ChecklistItem
          done={jobPosting.state === 'ready'}
          label="Job posting"
          detail={jobPostingDetail(jobPosting)}
        />
      </ul>

      <p id={disclosureId} className="text-sm text-slate-600">
        {GEMINI_DISCLOSURE}
      </p>
      <p className="text-sm text-slate-500">{RETENTION_SENTENCE}</p>

      <button
        type="button"
        onClick={onLaunch}
        disabled={reason !== null || isStarting}
        aria-describedby={reason !== null ? reasonId : disclosureId}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {isStarting ? 'Starting…' : hasPreviousRun ? 'Tailor again' : 'Tailor my CV'}
      </button>

      {reason !== null && (
        <p id={reasonId} className="text-sm text-slate-600">
          {reason}
        </p>
      )}
    </div>
  );
}
