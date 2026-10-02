/**
 * `/reset-password/confirm` (AC-48) — public. Reads the token from the fragment once and strips it,
 * as `/confirm-email` does.
 *
 * - **Empty:** no token → *"This link is incomplete. …"* and no form.
 * - **Idle:** a labelled new-password field with 2.1's hint, and **Save new password**.
 * - **Pending:** *"Saving your new password…"*, disabled; a double click posts once.
 * - **204:** `role="status"` *"Your password has been changed and you've been logged out
 *   everywhere."* + **Log in**. An authenticated tab signs out with reason `password_changed`
 *   (no refresh call), and other tabs follow through 2.2's `BroadcastChannel`.
 * - **400 `link_invalid`:** *"This reset link has expired or has already been used."* + **Send a new
 *   link** → `/reset-password`.
 * - **422 `password_*`:** the server's bound (2.1's copy); the token is kept, the form stays.
 * - **503 / network:** distinct from pending; the button returns.
 *
 * SKELETON (T36): renders nothing; T38 builds it.
 */
export function PasswordResetConfirmPage(): React.JSX.Element | null {
  return null;
}
