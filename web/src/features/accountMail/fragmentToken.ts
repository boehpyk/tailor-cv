import { useLayoutEffect, useState } from 'react';

/**
 * The one-time token a confirmation or reset link carries in its **fragment** (`#token=…`) — read
 * once, kept in memory, and wiped from the address bar before the first paint (technical plan §7,
 * AC-46, AC-48, AC-51).
 *
 * **Why the fragment.** A browser never sends it to a server, so the token is in no access log, no
 * `Referer` and no proxy. That protection ends the moment it sits in the address bar, the history
 * entry or a bookmark — hence the strip.
 *
 * **Why a hook with an effect.** The address bar is outside React; synchronising with it is exactly
 * what an effect is for. `useLayoutEffect` rather than `useEffect`, so the token leaves the URL
 * before the browser paints the page that read it.
 *
 * **Where the value lives.** A `useState` initializer: it runs on the first render only, so the
 * value survives the strip. Never a query, never browser storage. A reload after the strip reads no
 * fragment and returns `null` — on purpose; the page's empty state says to open the link again.
 *
 * **StrictMode.** React may call the initializer twice and run the effect twice. Both initializer
 * calls happen before any effect, so they read the same hash; the effect strips only while a hash
 * is present, so its second run finds none and does nothing. One read, one `replaceState`.
 */
export function useFragmentToken(): string | null {
  const [token] = useState(readFragmentToken);

  useLayoutEffect(() => {
    if (window.location.hash !== '') {
      // Keep the path (the page must still be this page); drop the fragment. `search` is dropped
      // too: none of these links carries one, and nothing the app builds should either.
      window.history.replaceState(null, '', window.location.pathname);
    }
  }, []);

  return token;
}

/** `#token=abc` → `"abc"`; no fragment, no `token`, or an empty one → `null`. */
function readFragmentToken(): string | null {
  const hash = window.location.hash;
  if (hash.length <= 1) {
    return null;
  }
  const token = new URLSearchParams(hash.slice(1)).get('token');
  return token === null || token === '' ? null : token;
}
