/**
 * The Delete account section on `/account` (AC-40, AC-46).
 *
 * States what goes (the account, the saved CVs and their files, every signed-in device) and what
 * does not go at once (workspace copies, within 24 hours). A labelled password field
 * (`autocomplete="current-password"`), an "I understand this can't be undone" checkbox that gates
 * the button, "Deleting your account…" while pending, and each refusal in its own words — none of
 * which signs the user out, because none of them deleted anything. On success (`useDeleteAccount`
 * has already signed this tab out and told the others) it navigates to `/` with the notice "Your
 * account and saved CVs were deleted."
 *
 * The password and the checkbox are local form state; the password is never put anywhere else.
 *
 * SKELETON (T25): renders nothing. GREEN is T27; placed on `AccountPage` at T28.
 */
export function DeleteAccountSection(): React.JSX.Element | null {
  return null;
}
