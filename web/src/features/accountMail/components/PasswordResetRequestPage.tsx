/**
 * `/reset-password` (AC-47) — public; linked from `/login`'s **Forgot your password?**.
 *
 * - **Idle:** a labelled email field and **Send reset link**.
 * - **Pending:** *"Sending…"*, disabled; a same-tick double click posts once (`isMutating` on
 *   `requestPasswordResetMutationKey`, V-60).
 * - **202:** `role="status"` *"If there's an account for {email}, …"* + the mail-provider sentence —
 *   the same whether or not the account exists.
 * - **Failures** (`role="alert"`), each distinct: 422 `invalid_email`, 429 with the server's
 *   seconds, 503, network.
 *
 * SKELETON (T36): renders nothing; T38 builds it.
 */
export function PasswordResetRequestPage(): React.JSX.Element | null {
  return null;
}
