/**
 * Every sentence the admin screen puts on screen (slice 4.1, AC-31, AC-33), in one module so a test
 * imports the string it pins instead of retyping it. The not-found view is `NotFoundPage`'s own
 * copy (OQ-2): nothing here says "forbidden".
 */

/** AC-31: the probe is pending (`role="status"`). */
export const ADMIN_CHECKING = 'Checking access…';
/** AC-31: the shell's `<h1>`, which labels its `<section>` (AC-36). */
export const ADMIN_HEADING = 'Admin';
/** AC-31: the empty state. 4.2 replaces it with the user list. Must stay out of the main chunk (AC-35). */
export const ADMIN_EMPTY = 'Nothing to manage here yet.';
/** AC-31: 503 or a network failure (`role="alert"`), visibly different from checking. */
export const ADMIN_UNAVAILABLE = "Couldn't check admin access.";
export const ADMIN_RETRY_LABEL = 'Retry';

/** AC-33: the `Suspense` fallback while the lazy chunk loads (`role="status"`). */
export const LAZY_LOADING = 'Loading…';
/** AC-33: the chunk failed to load (a tab older than the last release). */
export const LAZY_CHUNK_FAILED = "Couldn't load this page. Reload to get the latest version.";
export const LAZY_RELOAD_LABEL = 'Reload';
