import { Link } from 'react-router';

import { authErrorCopy } from '../authCopy';

import type { AuthAction } from '../authCopy';

export interface AuthErrorNoticeProps {
  /**
   * The element id, so the form's inputs can point at it with `aria-describedby` (AC-45). Given
   * by the form, which owns both ends of the link.
   */
  readonly id: string;
  /** Which form this notice answers — the same code reads differently on each. */
  readonly action: AuthAction;
  /** The mutation's `error`; `null` renders nothing. */
  readonly error: Error | null;
}

/**
 * One `role="alert"` notice per form, its sentence from `authErrorCopy(action, error)` (AC-39,
 * AC-40).
 *
 * `role="alert"` is announced the moment it is inserted, which is exactly when a refusal arrives —
 * so the notice is rendered only while there is an error, not kept in the tree empty. The message
 * sits in its own element so the sentence is one text node whatever link follows it.
 */
export function AuthErrorNotice({ id, action, error }: AuthErrorNoticeProps) {
  if (error === null) {
    return null;
  }
  const copy = authErrorCopy(action, error);
  return (
    <div
      id={id}
      role="alert"
      className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
    >
      <span>{copy.message}</span>
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
