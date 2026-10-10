/**
 * The copy of a lazy route's two non-content states (slice 4.1, AC-33). Its own module because the
 * main bundle renders these (the `Suspense` fallback and `LazyBoundary`) while `adminCopy.ts`'s own
 * sentences must stay in the lazy chunk (AC-35): a module imported from both chunks lands whole in
 * the main one.
 */

/** AC-33: the `Suspense` fallback while the lazy chunk loads (`role="status"`). */
export const LAZY_LOADING = 'Loading…';
/** AC-33: the chunk failed to load (a tab older than the last release). */
export const LAZY_CHUNK_FAILED = "Couldn't load this page. Reload to get the latest version.";
export const LAZY_RELOAD_LABEL = 'Reload';
