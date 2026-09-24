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
 * AC-40). **T40 SKELETON** — nothing without an error; with one, an empty element carrying the id
 * and no role and no text.
 */
export function AuthErrorNotice({ id, action, error }: AuthErrorNoticeProps) {
  if (error === null) {
    return null;
  }
  return <div id={id} data-auth-action={action} />;
}
