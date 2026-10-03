import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render } from '@testing-library/react';
import { StrictMode } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { jsonResponse } from '@/test/fixtures';

import type { RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';

/**
 * Slice 2.5 test support (T37): the shared fixtures and renderers of the account-mail screens'
 * tests.
 *
 * **A fragment lives on `window.location`, not on the router.** `useFragmentToken` reads the real
 * address bar, so `openLink` sets jsdom's URL with `history.replaceState` *before* the render, and
 * every test restores it (`resetAccountMailTest`) — a token left in the address bar would be read
 * by the next test's page.
 *
 * **The client keeps what it is given** (`gcTime: Infinity`): an absence assertion on the cache
 * must test the code, not the collector (CLAUDE.md, 2.1's trap).
 */

/** A recognisable token and address, so a leak into a URL, a key or a store can be grepped for. */
export const TOKEN = 'TOKENMARKER-7f3a9c2e41';
export const EMAIL = 'alex.marker@example.com';
export const PASSWORD = 'a very long real password';

export function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

/** Put the address bar where an email link would have: `path` and, when given, `#token=…`. */
export function openLink(path: string, token?: string): void {
  window.history.replaceState(null, '', token === undefined ? path : `${path}#token=${token}`);
}

/** Back to a clean, anonymous slate — call from `beforeEach`/`afterEach`. */
export function resetAccountMailTest(): void {
  window.history.replaceState(null, '', '/');
  __resetForTests();
  authStore.signOut('expired');
}

/** Renders where the router is, so a navigation is observable without the real route table. */
// eslint-disable-next-line react-refresh/only-export-components -- test support, never hot-reloaded: a probe beside plain helpers
function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <p data-testid="location">{`${location.pathname}${location.search}`}</p>;
}

export interface RenderPageOptions {
  /** The route pattern the element is mounted at (default `/`). */
  readonly routePath?: string;
  /** The memory router's initial entry (default `routePath`). */
  readonly entry?: string;
  readonly client?: QueryClient;
  /** Mount under `<StrictMode>` (double render, double effect). */
  readonly strict?: boolean;
}

export interface RenderedPage extends RenderResult {
  readonly client: QueryClient;
  /** `pathname + search` the memory router is at now. */
  readonly location: () => string;
}

/** One page on its own `MemoryRouter`, with a probe for where it navigated. No app layout. */
export function renderPage(element: ReactElement, options: RenderPageOptions = {}): RenderedPage {
  const routePath = options.routePath ?? '/';
  const client = options.client ?? newClient();
  const tree = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[options.entry ?? routePath]}>
        <Routes>
          <Route path={routePath} element={element} />
          <Route path="*" element={null} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const utils = render(options.strict === true ? <StrictMode>{tree}</StrictMode> : tree);
  return {
    ...utils,
    client,
    location: () =>
      utils.container.ownerDocument.querySelector('[data-testid="location"]')?.textContent ?? '',
  };
}

/**
 * Two activations inside **one** `act`, so React flushes nothing between them: `isPending` has not
 * re-rendered, the button is not yet disabled, and only a guard on the mutation cache
 * (`isMutating`) can stop the second (2.1's I-53 trap; a click per `fireEvent` would let the
 * disabled button do the guard's job and prove nothing).
 */
export function activateTwiceInOneTick(element: HTMLElement): void {
  act(() => {
    element.click();
    element.click();
  });
}

export const hangForever = (): Promise<Response> => new Promise<Response>(() => undefined);

export const networkFailure = (): Promise<Response> =>
  Promise.reject(new TypeError('Failed to fetch'));

export function refusal(
  status: number,
  code: string,
  extra: object = {},
  headers?: Record<string, string>,
) {
  return (): Response =>
    jsonResponse(status, { error: { code, message: 'server prose', ...extra } }, headers);
}

export const accepted = (): Response => new Response(null, { status: 202 });
export const noContent = (): Response => new Response(null, { status: 204 });

/** Every string the browser keeps for this page: both storages, as one greppable blob. */
export function storageDump(): string {
  const parts: string[] = [];
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index);
      if (key !== null) {
        parts.push(key, store.getItem(key) ?? '');
      }
    }
  }
  return parts.join('\n');
}
