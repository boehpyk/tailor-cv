import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { ACCOUNT_DISABLED_NOTICE } from '@/features/auth/authCopy';
import { ACCOUNT_DELETED_NOTICE } from '@/features/savedCvs/savedCvsCopy';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  hang,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import {
  COUNTS,
  GUEST_HOME,
  REPORT,
  adminOnly,
  detail,
  firewallNotFound,
  noContent,
  page,
  userGone,
} from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import {
  ACTION_FAILED,
  BACK_TO_USERS,
  COUNT_LABELS,
  DISABLE_DIALOG_TITLE,
  DISABLE_LABEL,
  DISABLE_PENDING_LABEL,
  ENABLE_LABEL,
  ERASE_DIALOG_TITLE,
  ERASE_LABEL,
  OWN_ACCOUNT_NOTE,
  ROLE_READ_ONLY_NOTE,
  USER_ERROR,
  USER_GONE,
  USER_LOADING,
  disableDialogMessage,
  eraseDialogMessage,
  erasedNotice,
  unlinkFailuresNote,
} from '../adminCopy';

import type { AccountCounts, AdminUserDetail } from '@/api/admin';
import type { RouteHandler } from '@/test/accountFetch';
import type { User } from '@/features/auth/types';

/**
 * T20 RED — slice 4.2, AC-33 (one account), AC-34 (disable / enable) and AC-35 (erase), mounted
 * through the real route table. Written from the spec; against T19's skeleton (`UserPage` is a
 * fixed "Loading…" status that fetches nothing) each fails on an absent element.
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
const ANN = detail({ id: 'u-ann', email: 'ann@example.com' });
const SELF = detail({ id: ADMIN.id, email: ADMIN.email, role: 'admin' });

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

interface Overrides {
  readonly get?: RouteHandler;
  readonly disable?: RouteHandler;
  readonly enable?: RouteHandler;
  readonly erase?: RouteHandler;
}

/** A stateful fake: disable/enable flip `disabled_at`; erase removes the account from both reads. */
function serve(
  target: AdminUserDetail,
  overrides: Overrides = {},
): ReturnType<typeof stubAccountFetch> {
  let current: AdminUserDetail | null = target;
  const change = (disabledAt: string | null): Response => {
    if (current === null) return userGone();
    current = { ...current, disabled_at: disabledAt };
    return Response.json({
      user_id: current.id,
      disabled_at: disabledAt,
      changed: true,
      logins_revoked: 0,
    });
  };
  const fetch = stubAccountFetch({
    ...signedInRoutes(ADMIN),
    ...GUEST_HOME,
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': adminOnly(ADMIN, (call, n) =>
      page(current === null ? [] : [current])(call, n),
    ),
    'GET /api/admin/users/:id': adminOnly(
      ADMIN,
      overrides.get ?? (() => (current === null ? userGone() : Response.json(current))),
    ),
    // Slice 4.3 (owner's amendment, harness-only): the page now mounts the account's content lists.
    'GET /api/admin/users/:id/base-cvs': adminOnly(ADMIN, ok({ items: [] })),
    'GET /api/admin/users/:id/job-postings': adminOnly(ADMIN, ok({ items: [] })),
    'GET /api/admin/users/:id/tailoring-runs': adminOnly(
      ADMIN,
      ok({ items: [], next_cursor: null }),
    ),
    'POST /api/admin/users/:id/disable': adminOnly(
      ADMIN,
      overrides.disable ?? (() => change('2026-10-11T08:00:00Z')),
    ),
    'POST /api/admin/users/:id/enable': adminOnly(ADMIN, overrides.enable ?? (() => change(null))),
    'DELETE /api/admin/users/:id': adminOnly(
      ADMIN,
      overrides.erase ??
        (() => {
          current = null;
          return Response.json(REPORT);
        }),
    ),
  });
  signInAs(ADMIN);
  return fetch;
}

const open = (id: string, queryClient = createAppQueryClient()) =>
  renderWithRouter(`/admin/users/${id}`, { queryClient });

async function openDialog(button: string): Promise<HTMLElement> {
  const control = await screen.findByRole('button', { name: button });
  await waitFor(() => {
    expect(control).toBeEnabled();
  });
  await userEvent.setup().click(control);
  return screen.findByRole('alertdialog');
}

describe('/admin/users/:userId — AC-33', () => {
  it('loading: a status "Loading…"', async () => {
    serve(ANN, { get: hang });
    open('u-ann');

    expect(await screen.findByText(USER_LOADING)).toHaveAttribute('role', 'status');
  });

  it('overview: email as h1, id, the read-only role sentence and the status', async () => {
    serve(ANN);
    open('u-ann');

    expect(
      await screen.findByRole('heading', { level: 1, name: 'ann@example.com' }),
    ).toBeInTheDocument();
    expect(screen.getByText('u-ann')).toBeInTheDocument();
    expect(screen.getByText(ROLE_READ_ONLY_NOTE)).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText(USER_LOADING)).not.toBeInTheDocument();
  });

  it('the seven counts are a definition list, each number under its own label', async () => {
    serve(ANN);
    open('u-ann');

    await screen.findByRole('heading', { level: 1, name: 'ann@example.com' });
    for (const key of Object.keys(COUNT_LABELS) as (keyof AccountCounts)[]) {
      const term = screen.getByText(COUNT_LABELS[key], { selector: 'dt' });
      expect(term.nextElementSibling).toHaveTextContent(new RegExp(`^${String(COUNTS[key])}$`));
    }
  });

  it('404 user_not_found: "This account no longer exists." and a link back to the list', async () => {
    serve(ANN, { get: userGone });
    open('u-ann');

    expect(await screen.findByText(USER_GONE)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: BACK_TO_USERS })).toHaveAttribute('href', '/admin');
  });

  it('a code-less 404 (the firewall) renders the app not-found page, not "no longer exists"', async () => {
    serve(ANN, { get: firewallNotFound });
    open('u-ann');

    expect(
      await screen.findByRole('heading', { name: /couldn.t find that page/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(USER_GONE)).not.toBeInTheDocument();
  });

  it('error: an alert with Retry; Retry refetches and shows the overview', async () => {
    const fetch = serve(ANN, {
      get: (_call, n) => (n <= 2 ? status(503, 'service_unavailable')() : Response.json(ANN)),
    });
    open('u-ann');

    const alert = await screen.findByRole('alert', {}, { timeout: 4000 });
    expect(alert).toHaveTextContent(USER_ERROR);
    expect(screen.queryByText(USER_LOADING)).not.toBeInTheDocument();
    const before = callsTo(fetch, 'GET', '/api/admin/users/u-ann').length;
    await userEvent.setup().click(within(alert).getByRole('button', { name: /retry/i }));

    expect(
      await screen.findByRole('heading', { level: 1, name: 'ann@example.com' }),
    ).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/admin/users/u-ann').length).toBeGreaterThan(before);
  });

  it('a signed-in plain user is refused by the fake and sees the not-found page (per-bearer isolation)', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/admin/access': firewallNotFound,
      'GET /api/admin/users/:id': adminOnly(ADMIN, () => Response.json(ANN)),
    });
    signInAs(USER_A);
    open('u-ann');

    expect(
      await screen.findByRole('heading', { name: /couldn.t find that page/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText('ann@example.com')).not.toBeInTheDocument();
  });
});

describe('Disable and enable — AC-34', () => {
  it('Disable opens a dialog naming the email and saying the user is signed out; nothing is sent yet', async () => {
    const fetch = serve(ANN);
    open('u-ann');

    const dialog = await openDialog(DISABLE_LABEL);

    expect(within(dialog).getByText(DISABLE_DIALOG_TITLE)).toBeInTheDocument();
    expect(dialog).toHaveTextContent(disableDialogMessage('ann@example.com', false));
    expect(dialog).not.toHaveTextContent(OWN_ACCOUNT_NOTE);
    expect(callsTo(fetch, 'POST', '/api/admin/users/:id/disable')).toHaveLength(0);
  });

  it('confirming POSTs disable, then the status reads Disabled and the button becomes Enable', async () => {
    const fetch = serve(ANN);
    open('u-ann');
    const dialog = await openDialog(DISABLE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: DISABLE_LABEL }));

    expect(await screen.findByRole('button', { name: ENABLE_LABEL })).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: DISABLE_LABEL })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/admin/users/u-ann/disable')).toHaveLength(1);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('not optimistic: while the POST is in flight the status still reads Active', async () => {
    serve(ANN, { disable: hang });
    open('u-ann');
    const dialog = await openDialog(DISABLE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: DISABLE_LABEL }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: DISABLE_PENDING_LABEL })).toBeDisabled();
    });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
  });

  it('Enable asks no confirmation: one click, one POST, the status reads Active again', async () => {
    const fetch = serve({ ...ANN, disabled_at: '2026-10-01T09:00:00Z' });
    open('u-ann');
    const enable = await screen.findByRole('button', { name: ENABLE_LABEL });
    await waitFor(() => {
      expect(enable).toBeEnabled();
    });

    await userEvent.setup().click(enable);

    expect(await screen.findByRole('button', { name: DISABLE_LABEL })).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/admin/users/u-ann/enable')).toHaveLength(1);
  });

  it('a refused disable shows an alert in place and leaves the button usable; it is not re-sent', async () => {
    const fetch = serve(ANN, { disable: status(500, 'internal_error') });
    open('u-ann');
    const dialog = await openDialog(DISABLE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: DISABLE_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(ACTION_FAILED.disable);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: DISABLE_LABEL })).toBeEnabled();
    });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/admin/users/u-ann/disable')).toHaveLength(1);
  });

  it("on the actor's own account the dialog adds the sign-out sentence, and success lands on / with the notice", async () => {
    serve(SELF);
    const { router } = open(ADMIN.id);
    const dialog = await openDialog(DISABLE_LABEL);
    expect(dialog).toHaveTextContent(OWN_ACCOUNT_NOTE);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: DISABLE_LABEL }));

    expect(await screen.findByText(ACCOUNT_DISABLED_NOTICE)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/');
  });
});

describe('Erase — AC-35', () => {
  it('Erase opens a dialog naming the email and every count, saying it cannot be undone; nothing is sent yet', async () => {
    const fetch = serve(ANN);
    open('u-ann');

    const dialog = await openDialog(ERASE_LABEL);

    expect(within(dialog).getByText(ERASE_DIALOG_TITLE)).toBeInTheDocument();
    expect(dialog).toHaveTextContent(eraseDialogMessage('ann@example.com', COUNTS, false));
    expect(dialog).not.toHaveTextContent(OWN_ACCOUNT_NOTE);
    expect(callsTo(fetch, 'DELETE', '/api/admin/users/:id')).toHaveLength(0);
  });

  it('confirming DELETEs, lands on /admin with the notice, and the list no longer shows the account', async () => {
    const fetch = serve(ANN);
    const queryClient = createAppQueryClient();
    const { router } = renderWithRouter('/admin', { queryClient });
    // The list is cached with Ann in it, so only an invalidation can remove her.
    await userEvent.setup().click(await screen.findByRole('link', { name: 'ann@example.com' }));
    const dialog = await openDialog(ERASE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: ERASE_LABEL }));

    expect(await screen.findByText(erasedNotice('ann@example.com'))).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/admin');
    expect(callsTo(fetch, 'DELETE', '/api/admin/users/u-ann')).toHaveLength(1);
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'ann@example.com' })).not.toBeInTheDocument();
    });
    expect(
      queryClient.getQueryState(['auth', 'account', ADMIN.id, 'admin', 'user', 'u-ann']),
    ).toBeUndefined();
  });

  it('files that could not be unlinked are named, with the orphan sweep', async () => {
    serve(ANN, { erase: ok({ ...REPORT, files_unlinked: 0, unlink_failures: 2 }) });
    open('u-ann');
    const dialog = await openDialog(ERASE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: ERASE_LABEL }));

    expect(await screen.findByText(unlinkFailuresNote(2))).toBeInTheDocument();
  });

  it("on the actor's own account the dialog says so, and success lands on / with the deleted notice", async () => {
    serve(SELF);
    const { router } = open(ADMIN.id);
    const dialog = await openDialog(ERASE_LABEL);
    expect(dialog).toHaveTextContent(OWN_ACCOUNT_NOTE);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: ERASE_LABEL }));

    expect(await screen.findByText(ACCOUNT_DELETED_NOTICE)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/');
  });

  it('a 404 on the erase says "This account no longer exists."', async () => {
    serve(ANN, { erase: userGone });
    open('u-ann');
    const dialog = await openDialog(ERASE_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: ERASE_LABEL }));

    expect(await screen.findByText(USER_GONE)).toBeInTheDocument();
  });
});
