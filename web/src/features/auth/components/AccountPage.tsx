/**
 * `/account` (AC-41), rendered inside `RequireAuth`. **T40 SKELETON** — a heading and nothing
 * else.
 *
 * No props: the profile is `useCurrentUser`'s query (loading / error + Retry / success), and
 * **Log out** is `useLogout` with its own pending and error states.
 */
export function AccountPage() {
  return <h1>Your account</h1>;
}
