import { Link } from 'react-router';

import { focusOnMount as focusOutcome } from '@/components/ui/focusOnMount';

import { authErrorCopy } from '../authCopy';

import type { AuthAction } from '../authCopy';

export interface AuthErrorNoticeProps {
  /** Move focus to the notice when it appears (the outcome of a submit). Default `false`. */
  readonly focusOnMount?: boolean;
  /**
   * The element id, so the form's inputs can point at it with `aria-describedby` (AC-45). Given
   * by the form, which owns both ends of the link.
   */
  readonly id: string;
  /** Which form this notice answers — the same code reads differently on each. */
  readonly action: AuthAction;
  /** The mutation's `error`; `null` renders nothing. */
  readonly error: Error | null;
  /** A 429's wait in words (`useErrorHold(error).phrase`), for the *Too many attempts* line. */
  readonly retryWhen?: string | null;
}

/**
 * One `role="alert"` notice per form, its sentence from `authErrorCopy(action, error)` (AC-39,
 * AC-40).
 *
 * `role="alert"` is announced the moment it is inserted, which is exactly when a refusal arrives —
 * so the notice is rendered only while there is an error, not kept in the tree empty. The message
 * sits in its own element so the sentence is one text node whatever note or link follows it.
 *
 * `focusOnMount` moves focus to the notice when it appears (slice 2.5's screens, AC-52): the
 * outcome of a submit is where a keyboard user should be. 2.1's forms leave focus where it is.
 */
export function AuthErrorNotice({
  id,
  action,
  error,
  retryWhen = null,
  focusOnMount = false,
}: AuthErrorNoticeProps) {
  if (error === null) {
    return null;
  }
  const copy = authErrorCopy(action, error, retryWhen);
  return (
    <div
      id={id}
      role="alert"
      tabIndex={focusOnMount ? -1 : undefined}
      ref={focusOnMount ? focusOutcome : undefined}
      className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
    >
      <span>{copy.message}</span>
      {copy.note !== undefined && (
        <>
          {' '}
          <span>{copy.note}</span>
        </>
      )}
      {copy.link !== undefined && (
        <>
          {' '}
          <Link to={copy.link.to} className="font-medium underline underline-offset-2">
            {copy.link.label}
          </Link>
        </>
      )}
    </div>
  );
}
