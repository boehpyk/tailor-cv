import { useLayoutEffect, useState } from 'react';

/**
 * One link the page was handed: the token its fragment carried (or `null`), and which link this is
 * in the page's life. `generation` starts at 0 for the link the page mounted with and goes up by
 * one for every later link followed **in the same document** (a `hashchange`). It is the identity a
 * screen keys its outcome on, so a second link starts from the ready state — keyed on a counter,
 * not on the token, so a credential is never used as an identity and the same link opened twice is
 * still two visits.
 */
export interface FragmentLink {
  readonly token: string | null;
  readonly generation: number;
}

/**
 * The one-time token a confirmation or reset link carries in its **fragment** (`#token=…`) — read
 * once per link, kept in memory, and wiped from the address bar (technical plan §7, AC-46, AC-48,
 * AC-51).
 *
 * **Why the fragment.** A browser never sends it to a server, so the token is in no access log, no
 * `Referer` and no proxy. That protection ends the moment it sits in the address bar, the history
 * entry or a bookmark — hence the strip.
 *
 * **Why a hook with an effect.** The address bar is outside React; synchronising with it is exactly
 * what an effect is for. `useLayoutEffect` rather than `useEffect`, so the token the page mounted
 * with leaves the URL before the browser paints the page that read it.
 *
 * **A second link in the same tab** (/verify r1, found in a real browser). Going from
 * `/confirm-email#token=A` to `/confirm-email#token=B` changes only the hash: nothing remounts and
 * no initializer runs again, so without a subscription B was never read and **never stripped**.
 * The effect therefore also subscribes to `hashchange` — the browser's event for exactly this — and
 * the handler reads the new token, strips it with the same `replaceState`, and stores it with the
 * next `generation`. The strip happens in the handler, before React renders anything for it. Our
 * own `replaceState` fires no `hashchange`, so the handler never re-enters itself.
 *
 * **Where the value lives.** `useState`: the initializer reads the mount's link on the first render
 * only, so the value survives the strip; the handler replaces it. Never a query, never browser
 * storage. A reload after the strip reads no fragment and returns `null` — on purpose; the page's
 * empty state says to open the link again.
 *
 * **StrictMode.** React may call the initializer twice and run the effect twice. Both initializer
 * calls happen before any effect, so they read the same hash; the effect strips only while a hash
 * is present, so its second run finds none and does nothing. One read, one `replaceState`. The
 * listener is removed by the first run's cleanup, so exactly one is ever attached.
 */
export function useFragmentLink(): FragmentLink {
  const [link, setLink] = useState<FragmentLink>(() => ({
    token: readFragmentToken(),
    generation: 0,
  }));

  useLayoutEffect(() => {
    stripFragment();

    function onHashChange(): void {
      if (window.location.hash === '') {
        return;
      }
      // Read, then strip, then store: the updater stays pure (StrictMode may call it twice).
      const token = readFragmentToken();
      stripFragment();
      setLink((previous) => ({ token, generation: previous.generation + 1 }));
    }

    window.addEventListener('hashchange', onHashChange);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
    };
  }, []);

  return link;
}

/** The token alone, for a caller that never needs to tell two links apart. */
export function useFragmentToken(): string | null {
  return useFragmentLink().token;
}

function stripFragment(): void {
  if (window.location.hash !== '') {
    // Keep the path (the page must still be this page); drop the fragment. `search` is dropped
    // too: none of these links carries one, and nothing the app builds should either.
    window.history.replaceState(null, '', window.location.pathname);
  }
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
