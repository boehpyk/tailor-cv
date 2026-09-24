/**
 * The words the auth screens say, kept out of the components that say them — `exportCopy.ts`'s
 * pattern (technical plan §7, AC-39, AC-40).
 *
 * **T40 SKELETON.** The types are real; the lookup throws. T42 GREEN writes the table: one sentence
 * per `AuthErrorCode` per action, exhaustive by type over the closed set in `types.ts`, plus the
 * two error shapes outside it — no answer at all ("Couldn't reach TailorCraft.") and a code this
 * client does not know (a fallback, because `ApiError.code` is an open set).
 *
 * The password-length hint on `/register` is **copy, not a rule**: the API is the authority and
 * returns the number in `ApiError.details` (Constitution §4.5), and the refusal's sentence is built
 * from that number, never from a constant here.
 */

/** Which form a refusal answered — the same code reads differently on each ("Logging in is…"). */
export type AuthAction = 'login' | 'register' | 'logout';

/**
 * One notice's content. `link` is set where the notice offers a way forward in words the user can
 * click — `email_already_registered` → **Log in** (AC-40).
 */
export interface AuthErrorCopy {
  readonly message: string;
  readonly link?: { readonly to: string; readonly label: string };
}

/**
 * The notice for `error` on `action`'s form. Takes the whole error, not just its `code`, because
 * two sentences carry a number the server sent: `rate_limited` (`retryAfterSeconds`) and
 * `password_too_short` / `password_too_long` (`details.min_length` / `details.max_length`).
 */
export function authErrorCopy(action: AuthAction, error: Error): AuthErrorCopy {
  throw new Error(
    `authErrorCopy(${action}, ${error.name}): not implemented (T40 skeleton; T42 GREEN)`,
  );
}
