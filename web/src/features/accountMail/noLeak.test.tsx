import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { stubAccountFetch } from '@/test/accountFetch';

import { RegisterPage } from '../auth/components/RegisterPage';
import { ConfirmEmailPage } from './components/ConfirmEmailPage';
import { PasswordResetConfirmPage } from './components/PasswordResetConfirmPage';
import { PasswordResetRequestPage } from './components/PasswordResetRequestPage';
import { confirmPasswordResetMutationKey } from './hooks/useConfirmPasswordReset';
import { confirmRegistrationMutationKey } from './hooks/useConfirmRegistration';
import { requestPasswordResetMutationKey } from './hooks/useRequestPasswordReset';
import { requestRegistrationMutationKey } from './hooks/useRequestRegistration';
import {
  EMAIL,
  PASSWORD,
  TOKEN,
  accepted,
  newClient,
  noContent,
  openLink,
  renderPage,
  resetAccountMailTest,
  storageDump,
} from './test/support';

import type { QueryClient } from '@tanstack/react-query';

/**
 * T37 RED — AC-51: no token and no address in any URL the app builds, in browser storage, or in the
 * query cache; the four new mutations live under `['auth', …]`; no HTML from content.
 *
 * **A grep needs a positive control, and so does each of these.** `leaks()` is the one detector
 * every assertion uses; its own test plants a marker in a URL, a storage entry and a query key and
 * watches it find all three — a detector that has never found anything proves nothing about a clean
 * run. The flow test's own positive control is that the four mutations *exist* in the mutation
 * cache afterwards: a skeleton renders `null`, runs nothing, and would otherwise pass every
 * absence assertion here.
 */

const MARKERS = [TOKEN, EMAIL, encodeURIComponent(EMAIL), PASSWORD] as const;

/** Every marker found in `haystack`. */
function leaks(haystack: string): string[] {
  return MARKERS.filter((marker) => haystack.includes(marker));
}

/** Everything the query cache holds: keys, data and the keys of in-flight state. */
function queryCacheDump(client: QueryClient): string {
  return JSON.stringify(
    client
      .getQueryCache()
      .getAll()
      .map((query) => ({ key: query.queryKey, data: query.state.data })),
  );
}

beforeEach(() => {
  resetAccountMailTest();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  resetAccountMailTest();
});

describe('the detector (positive control)', () => {
  it('finds a marker in a URL, in storage and in a query key', () => {
    const client = newClient();
    client.setQueryData(['leaky', EMAIL], 1);
    window.localStorage.setItem('k', `v-${TOKEN}`);

    expect(leaks(`/api/x?email=${encodeURIComponent(EMAIL)}`)).toEqual([encodeURIComponent(EMAIL)]);
    expect(leaks(storageDump())).toEqual([TOKEN]);
    expect(leaks(queryCacheDump(client))).toEqual([EMAIL]);
    expect(leaks('a clean string')).toEqual([]);
  });
});

describe('all four mail flows, end to end through the real pages', () => {
  it('leave no token, address or password in any request URL, storage, the query cache or the address bar — and ran four mutations under [auth, …]', async () => {
    const fetch = stubAccountFetch({
      'POST /api/auth/register': accepted,
      'POST /api/auth/registration/confirm': noContent,
      'POST /api/auth/password-reset': accepted,
      'POST /api/auth/password-reset/confirm': noContent,
      'GET /api/base-cvs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      'GET /api/tailoring-runs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
    });
    const client = newClient();
    const user = userEvent.setup();
    const addressBar: string[] = [];
    const replaceState = vi.spyOn(window.history, 'replaceState');

    // 1. Register, then Send it again.
    let page = renderPage(<RegisterPage />, { routePath: '/register', client });
    await user.type(screen.getByLabelText(/email/i), EMAIL);
    await user.type(screen.getByLabelText(/password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: /^create account$/i }));
    await user.click(await screen.findByRole('button', { name: /send it again/i }));
    await screen.findByText('Sent again.');
    addressBar.push(window.location.href, page.location());
    page.unmount();

    // 2. Confirm the registration.
    openLink('/confirm-email', TOKEN);
    page = renderPage(<ConfirmEmailPage />, { routePath: '/confirm-email', client });
    await user.click(await screen.findByRole('button', { name: /confirm my email address/i }));
    await screen.findByRole('status');
    addressBar.push(window.location.href, page.location());
    page.unmount();

    // 3. Ask for a reset.
    page = renderPage(<PasswordResetRequestPage />, { routePath: '/reset-password', client });
    await user.type(await screen.findByLabelText(/email/i), EMAIL);
    await user.click(screen.getByRole('button', { name: /send reset link/i }));
    await screen.findByRole('status');
    addressBar.push(window.location.href, page.location());
    page.unmount();

    // 4. Complete it.
    openLink('/reset-password/confirm', TOKEN);
    page = renderPage(<PasswordResetConfirmPage />, {
      routePath: '/reset-password/confirm',
      client,
    });
    await user.type(await screen.findByLabelText(/new password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: /save new password/i }));
    await screen.findByRole('status');
    addressBar.push(window.location.href, page.location());
    page.unmount();

    // Positive control: the flows really ran, as four mutations under ['auth', …].
    const keys = client
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.options.mutationKey);
    for (const key of [
      requestRegistrationMutationKey,
      confirmRegistrationMutationKey,
      requestPasswordResetMutationKey,
      confirmPasswordResetMutationKey,
    ]) {
      expect(keys).toContainEqual(key);
      expect(key[0]).toBe('auth');
    }
    expect(fetch.calls.filter((call) => call.method === 'POST').length).toBeGreaterThanOrEqual(5);

    // The absences.
    expect(
      leaks(fetch.calls.map((call) => `${call.path}?${call.query.toString()}`).join('\n')),
    ).toEqual([]);
    expect(leaks(addressBar.join('\n'))).toEqual([]);
    expect(leaks(storageDump())).toEqual([]);
    expect(leaks(queryCacheDump(client))).toEqual([]);
    expect(leaks(replaceState.mock.calls.map((call) => String(call[2])).join('\n'))).toEqual([]);
    // The query cache holds no query data of these flows at all but the guest lists the register
    // page reads; none of them carries a marker, and none is keyed under a mail-link route.
    expect(
      client
        .getQueryCache()
        .getAll()
        .every((query) => !JSON.stringify(query.queryKey).includes('registration')),
    ).toBe(true);
  });
});

describe('production code under features/accountMail/ builds no URL carrying a token', () => {
  const root = dirname(fileURLToPath(import.meta.url));

  function productionLines(dir: string): Array<{ path: string; text: string }> {
    const lines: Array<{ path: string; text: string }> = [];
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry !== 'test') {
          lines.push(...productionLines(full));
        }
      } else if (/\.tsx?$/.test(full) && !/\.test\.tsx?$/.test(full)) {
        for (const text of readFileSync(full, 'utf-8').split('\n')) {
          lines.push({ path: full, text });
        }
      }
    }
    return lines;
  }

  const isComment = (text: string): boolean => /^\s*(\/\/|\/\*|\*)/.test(text);
  /** A query-string token, or a fragment *written* (as opposed to read) by the app. */
  const buildsTokenUrl = (text: string): boolean =>
    !isComment(text) && /[?&]token=|[?&]email=|\/confirm[^'"`]*[?#]/.test(text);

  it('the detector flags a built token URL (positive control)', () => {
    expect(buildsTokenUrl('const url = `/confirm-email?token=${token}`;')).toBe(true);
    expect(buildsTokenUrl('navigate(`/reset-password/confirm#token=${t}`)')).toBe(true);
    expect(buildsTokenUrl('// see /confirm-email?token=… in the mail')).toBe(false);
  });

  it('finds no such line, and found the module to look at', () => {
    const lines = productionLines(root);

    expect(new Set(lines.map((line) => line.path)).size).toBeGreaterThanOrEqual(5);
    expect(lines.filter((line) => buildsTokenUrl(line.text)).map((line) => line.text)).toEqual([]);
  });
});
