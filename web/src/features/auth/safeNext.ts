/**
 * The `?next=` guard (AC-43, I-52) — where to send a user after they log in or register.
 *
 * `next` arrives in the URL, so it is **attacker-chosen**: a link to
 * `/login?next=https://evil.example` that sends a freshly-authenticated user to a look-alike page is
 * the classic open redirect, and it is most convincing exactly here, one click after a real login.
 *
 * The rule is an allow-list of one shape, not a deny-list of bad ones: a same-origin **path** —
 * `/` followed by anything but `/` or `\`. Everything else falls back to `/`:
 *
 * - `https://evil.example` — absolute URL, another origin.
 * - `//evil.example` — protocol-relative; a browser resolves it to another host.
 * - `/\evil.example` — browsers normalise `\` to `/` in special-scheme URLs, so this *is*
 *   `//evil.example` by the time it is navigated to.
 * - `javascript:alert(1)` — not a path at all.
 * - `''`, `null`, `undefined` — nothing asked for.
 *
 * Returns the path unchanged when it is safe; never throws.
 */
export function safeNext(next: string | null | undefined): string {
  throw new Error(
    `safeNext(${JSON.stringify(next ?? null)}): not implemented (T36 skeleton; T38 GREEN)`,
  );
}
