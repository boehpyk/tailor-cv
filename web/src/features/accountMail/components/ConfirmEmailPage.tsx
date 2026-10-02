/**
 * `/confirm-email` (AC-46) — public. Reads the token from the link's fragment once
 * (`useFragmentToken`) and strips it before paint.
 *
 * - **Empty:** no token → *"This link is incomplete. …"* and no button; no request (V-62, V-63).
 * - **Idle:** **Confirm my email address** — confirmation waits for a click (OQ-4), so a mail
 *   scanner prefetching the link confirms nothing.
 * - **Pending:** *"Confirming…"*, disabled; a double click posts once.
 * - **204:** `role="status"` *"Your email address is confirmed."* + **Log in** → `/login`. No sign-in.
 * - **400 `link_invalid`:** the expired-or-used sentence + **Log in** and **Create an account**.
 * - **409 `email_already_registered`:** the already-an-account sentence + **Log in** and **Reset your
 *   password**.
 * - **503 / network:** *"Couldn't confirm just now. Nothing changed — try again."* + the button,
 *   the token still in memory.
 *
 * SKELETON (T36): renders nothing; T38 builds it.
 */
export function ConfirmEmailPage(): React.JSX.Element | null {
  return null;
}
