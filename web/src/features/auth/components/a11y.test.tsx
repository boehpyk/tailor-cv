import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests } from '../authStore';
import { LoginPage } from './LoginPage';
import { RegisterPage } from './RegisterPage';

import type { ReactNode } from 'react';

/**
 * T44 (`qa`, test-after) — AC-45: every input on `/login` and `/register` has an associated
 * `<label>`, the right `autocomplete` value for a password manager, and — after a failed submit —
 * an error that is `role="alert"` and reachable from every input through `aria-describedby`.
 *
 * Written against the acceptance criterion, not against `CredentialsForm.tsx`'s source: each
 * assertion below is a property the markup must hold (a native `.labels` association, an exact
 * `autocomplete` string, an `aria-describedby` id that *resolves* through `document.getElementById`
 * to the element carrying `role="alert"`), never a copy of what the component happens to render.
 */

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

type FetchHandler = () => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, FetchHandler>): ReturnType<typeof vi.fn> {
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const handler = handlers[`${method} ${url}`];
    if (handler === undefined) {
      return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
    }
    return Promise.resolve(handler());
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderAt(element: ReactNode, path: string) {
  const client = makeQueryClient();

  function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  return render(
    <Routes>
      <Route path="/login" element={element} />
      <Route path="/register" element={element} />
    </Routes>,
    { wrapper: Wrapper },
  );
}

function fillForm(email = 'alex@example.com', password = 'a very long real password'): void {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: password } });
}

function submitButton(name: RegExp): HTMLElement {
  return screen.getByRole('button', { name });
}

/**
 * Every id an `aria-describedby` names, resolved through the real DOM — never string-compared
 * against an id the test constructed itself, which would prove nothing about whether the browser
 * (or a screen reader) could actually follow the link.
 */
function describedByIds(element: HTMLElement): string[] {
  return (element.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
}

/**
 * Resolves an input's `aria-describedby` to the element carrying `role="alert"`, if any of the
 * referenced ids does. `undefined` if none does — the caller asserts it is found.
 */
function resolveAlertDescription(input: HTMLElement): Element | undefined {
  for (const id of describedByIds(input)) {
    const target = document.getElementById(id);
    if (target?.getAttribute('role') === 'alert') {
      return target;
    }
  }
  return undefined;
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AC-45: /login accessibility', () => {
  it('idle: email and password each have a native label association and the right autocomplete', () => {
    makeFetchMock({});

    renderAt(<LoginPage />, '/login');

    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);

    // A native `.labels` association — not merely an accessible name — is what "an input has an
    // associated <label>" means: it is what a real browser uses to expand the click target and
    // what a screen reader announces on focus.
    expect((email as HTMLInputElement).labels?.length).toBeGreaterThan(0);
    expect((password as HTMLInputElement).labels?.length).toBeGreaterThan(0);

    expect(email).toHaveAttribute('autocomplete', 'email');
    expect(password).toHaveAttribute('autocomplete', 'current-password');
  });

  it('after a failed submit: the error is role="alert" and both inputs resolve to it via aria-describedby', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(401, { error: { code: 'invalid_credentials', message: 'server prose' } }),
    });

    renderAt(<LoginPage />, '/login');
    fillForm();
    fireEvent.click(submitButton(/log in/i));

    const alert = await screen.findByRole('alert');
    expect(alert.id).not.toBe('');

    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);

    const emailResolved = resolveAlertDescription(email);
    const passwordResolved = resolveAlertDescription(password);

    expect(emailResolved).toBeDefined();
    expect(passwordResolved).toBeDefined();
    // The id resolves to the *same* alert element the user sees, and its text is the one actually
    // rendered — not a hard-coded copy of the sentence, which would drift from `authCopy.ts`.
    expect(emailResolved?.textContent).toBe(alert.textContent);
    expect(passwordResolved?.textContent).toBe(alert.textContent);
  });
});

describe('AC-45: /register accessibility', () => {
  it('idle: email and password each have a native label association and the right autocomplete', () => {
    makeFetchMock({});

    renderAt(<RegisterPage />, '/register');

    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);

    expect((email as HTMLInputElement).labels?.length).toBeGreaterThan(0);
    expect((password as HTMLInputElement).labels?.length).toBeGreaterThan(0);

    expect(email).toHaveAttribute('autocomplete', 'email');
    // `new-password`, not `current-password` — the one attribute that tells a password manager to
    // *generate* rather than *fill* (CredentialsForm.tsx's own docstring names this as the reason
    // it exists).
    expect(password).toHaveAttribute('autocomplete', 'new-password');
  });

  it('after a failed submit: the error is role="alert" and both inputs resolve to it via aria-describedby', async () => {
    makeFetchMock({
      'POST /api/auth/register': () =>
        jsonResponse(422, {
          error: { code: 'password_too_short', message: 'server prose', min_length: 13 },
        }),
    });

    renderAt(<RegisterPage />, '/register');
    fillForm();
    fireEvent.click(submitButton(/create account/i));

    const alert = await screen.findByRole('alert');
    expect(alert.id).not.toBe('');

    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);

    const emailResolved = resolveAlertDescription(email);
    const passwordResolved = resolveAlertDescription(password);

    expect(emailResolved).toBeDefined();
    expect(passwordResolved).toBeDefined();
    expect(emailResolved?.textContent).toBe(alert.textContent);
    expect(passwordResolved?.textContent).toBe(alert.textContent);

    // On /register the password field also carries a hint (AC-45's own paragraph), so its
    // `aria-describedby` names *two* ids — the hint AND the error — never only one swapped in for
    // the other.
    expect(describedByIds(password).length).toBeGreaterThanOrEqual(2);
  });
});
