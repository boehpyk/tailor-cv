import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { useSavedBaseCvs } from '@/features/savedCvs/hooks/useSavedBaseCvs';
import {
  USER_A,
  bearerFor,
  callsTo,
  hang,
  ok,
  signInAs,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  CLAIM_FAILED,
  CLAIM_NOTHING_LEFT,
  KEEP_LABEL,
  KEEP_PENDING_LABEL,
  NOT_NOW_LABEL,
  OFFER_REGION_LABEL,
  OFFER_RETENTION_LINE,
} from '../claimCopy';
import {
  CLAIM_PATH,
  claimResult,
  guestCv,
  guestListRoutes,
  guestRun,
  newClient,
  renderOffer,
  workingCopy,
} from '../test/support';

import type { AccountFetch, RouteHandler } from '@/test/accountFetch';

/**
 * T30 RED — the claim offer (AC-37, AC-38, AC-39, AC-40, AC-42; C-37…C-41). A container: the guest
 * lists (read **without** the bearer) decide whether it shows, the claim is `POST
 * /api/me/guest-work/claim` with the bearer.
 *
 * Against T29's skeleton the component renders `null`. **A skeleton satisfies every absence
 * assertion**, so each "no offer" test below first waits for the guest lists to have been read (the
 * offer's only input) and the tests that show the region are its positive control.
 */

const OFFER = { name: OFFER_REGION_LABEL } as const;

function guestListsRead(fetch: AccountFetch): void {
  expect(callsTo(fetch, 'GET', '/api/base-cvs').length).toBeGreaterThan(0);
  expect(callsTo(fetch, 'GET', '/api/tailoring-runs').length).toBeGreaterThan(0);
}

async function findOffer(): Promise<HTMLElement> {
  return screen.findByRole('region', OFFER);
}

function claimRoutes(claim: RouteHandler, cvs = [guestCv()], runs = [guestRun()]) {
  return stubAccountFetch({ ...guestListRoutes(cvs, runs), [`POST ${CLAIM_PATH}`]: claim });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

// --- AC-37: idle ---------------------------------------------------------------------------------

describe('GuestWorkOffer — idle (AC-37)', () => {
  it('names each guest CV by filename and the number of tailored applications', async () => {
    stubAccountFetch(
      guestListRoutes(
        [
          guestCv({ id: 'a', original_filename: 'jane.pdf' }),
          guestCv({ id: 'b', original_filename: 'sam.docx' }),
        ],
        [guestRun({ id: 'r1' }), guestRun({ id: 'r2' })],
      ),
    );

    renderOffer();

    const region = await findOffer();
    expect(region).toHaveTextContent('jane.pdf');
    expect(region).toHaveTextContent('sam.docx');
    expect(region).toHaveTextContent('2 tailored applications');
  });

  it('states what keeping means and what not keeping means, with both buttons', async () => {
    stubAccountFetch(guestListRoutes([guestCv()], [guestRun()]));

    renderOffer();

    const region = await findOffer();
    expect(within(region).getByText(OFFER_RETENTION_LINE)).toBeInTheDocument();
    expect(OFFER_RETENTION_LINE).toBe(
      "Keep them in your account and they'll be saved until you delete them. Otherwise they're deleted within 24 hours.",
    );
    expect(within(region).getByRole('button', { name: KEEP_LABEL })).toBeEnabled();
    expect(within(region).getByRole('button', { name: NOT_NOW_LABEL })).toBeEnabled();
  });

  it('reads the guest lists WITHOUT the bearer', async () => {
    const fetch = stubAccountFetch(guestListRoutes([guestCv()], [guestRun()]));

    renderOffer();
    await findOffer();

    for (const path of ['/api/base-cvs', '/api/tailoring-runs']) {
      const [call] = callsTo(fetch, 'GET', path);
      expect(call, path).toBeDefined();
      expect(call?.authorization, path).toBeNull();
    }
  });

  it('offers a lone CV with no runs, and a run with no CV', async () => {
    stubAccountFetch(guestListRoutes([guestCv({ original_filename: 'only.pdf' })], []));
    const first = renderOffer();
    expect(await findOffer()).toHaveTextContent('only.pdf');
    first.unmount();
    vi.unstubAllGlobals();

    stubAccountFetch(guestListRoutes([], [guestRun()]));
    renderOffer();
    expect(await findOffer()).toHaveTextContent('1 tailored application');
  });

  it('does not name a working copy (a claim never moves one) but still offers the rest', async () => {
    stubAccountFetch(
      guestListRoutes([guestCv({ original_filename: 'jane.pdf' }), workingCopy()], [guestRun()]),
    );

    renderOffer();

    const region = await findOffer();
    expect(region).toHaveTextContent('jane.pdf');
    expect(region).not.toHaveTextContent('working-copy.pdf');
  });

  it('shows nothing when the lists are empty (the lists were read — positive control above)', async () => {
    const fetch = stubAccountFetch(guestListRoutes([], []));

    renderOffer();

    await waitFor(() => {
      guestListsRead(fetch);
    });
    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: KEEP_LABEL })).not.toBeInTheDocument();
  });

  it('shows nothing when the only guest CV is a working copy and there are no runs', async () => {
    const fetch = stubAccountFetch(guestListRoutes([workingCopy()], []));

    renderOffer();

    await waitFor(() => {
      guestListsRead(fetch);
    });
    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
  });

  it('C-37: a 401 from the guest lists is no offer and no refresh call', async () => {
    const fetch = stubAccountFetch({
      ...guestListRoutes([], []),
      'GET /api/base-cvs': status(401, 'guest_session_expired'),
      'GET /api/tailoring-runs': status(401, 'guest_session_expired'),
    });

    renderOffer();

    await waitFor(() => {
      guestListsRead(fetch);
    });
    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/auth/refresh')).toHaveLength(0);
  });

  it('a failed guest list is no offer either (an offer has no error state of its own)', async () => {
    const fetch = stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      'GET /api/base-cvs': status(503, 'service_unavailable'),
    });

    renderOffer();

    await waitFor(() => {
      guestListsRead(fetch);
    });
    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// --- AC-38: pending and success ---------------------------------------------------------------------

describe('GuestWorkOffer — pending and success (AC-38)', () => {
  it('posts once with the bearer and the cookie, and the button reads "Keeping your work…", disabled', async () => {
    const fetch = claimRoutes(hang);
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    const pending = await within(region).findByRole('button', { name: KEEP_PENDING_LABEL });
    expect(pending).toBeDisabled();
    const posts = callsTo(fetch, 'POST', CLAIM_PATH);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.authorization).toBe(bearerFor(USER_A));
    const postInit = fetch.mock.mock.calls
      .map((call) => call as [string | URL, RequestInit | undefined])
      .find(([input]) => String(input) === CLAIM_PATH)?.[1];
    expect(postInit?.credentials).toBe('include');
  });

  it('C-38: a same-tick double click posts once', async () => {
    const fetch = claimRoutes(hang);
    renderOffer();
    const region = await findOffer();
    const button = within(region).getByRole('button', { name: KEEP_LABEL });

    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => {
      expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
    });
    await within(region).findByRole('button', { name: KEEP_PENDING_LABEL });
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
  });

  it('on 200 shows a status note built from the SERVER’s counts', async () => {
    claimRoutes(ok(claimResult({ base_cvs: 1, tailoring_runs: 2 })), [guestCv()], [guestRun()]);
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    const note = await screen.findByRole('status');
    expect(note).toHaveTextContent('Kept in your account: 1 CV, 2 tailored applications.');
  });

  it('on 200 reports the server’s counts even when they differ from the offer’s estimate', async () => {
    // The offer named 1 CV and 1 run; the server moved 3 runs (one finished meanwhile).
    claimRoutes(ok(claimResult({ base_cvs: 1, tailoring_runs: 3 })), [guestCv()], [guestRun()]);
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('status')).toHaveTextContent('3 tailored applications');
  });

  it('on 200 calls onClaimed once, AFTER the guest roots are gone and the account root is stale', async () => {
    claimRoutes(ok(claimResult()));
    const queryClient = newClient();
    queryClient.setQueryData(['export', 'exportJobs', 'r1'], { items: ['seed'] });
    queryClient.setQueryData(['posting', 'jobPostings'], { items: ['seed'] });
    queryClient.setQueryData(['tailoring', 'tailoringRun', 'r1'], { id: 'r1' });
    queryClient.setQueryData(['auth', 'account', USER_A.id, 'history'], { items: ['seed'] });
    const seen: {
      exports: number;
      postings: number;
      runs: number;
      accountStale: boolean | undefined;
    }[] = [];
    const onClaimed = vi.fn(() => {
      seen.push({
        exports: queryClient.getQueryCache().findAll({ queryKey: ['export'] }).length,
        postings: queryClient.getQueryCache().findAll({ queryKey: ['posting'] }).length,
        runs: queryClient.getQueryCache().findAll({ queryKey: ['tailoring', 'tailoringRun'] })
          .length,
        accountStale: queryClient.getQueryState(['auth', 'account', USER_A.id, 'history'])
          ?.isInvalidated,
      });
    });
    const user = userEvent.setup();
    renderOffer({ queryClient, onClaimed });
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    await waitFor(() => {
      expect(onClaimed).toHaveBeenCalledTimes(1);
    });
    expect(onClaimed).toHaveBeenCalledWith(claimResult());
    expect(seen).toEqual([{ exports: 0, postings: 0, runs: 0, accountStale: true }]);
  });

  it('on 200 the account’s saved CVs and the account root are re-read (the saved list lives outside the account root)', async () => {
    // First read of the saved list: empty; after the claim: the claimed CV. If only the account
    // root were invalidated, the probe below would keep saying "No saved CVs".
    const claimed = {
      id: 'saved-1',
      label: null,
      original_filename: 'claimed-jane.pdf',
      content_type: 'application/pdf',
      size_bytes: 2048,
      status: 'extracted',
      character_count: 512,
      failure_reason: null,
      failure_message: null,
      uploaded_at: '2026-09-20T10:00:00Z',
    };
    const fetch = stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      'GET /api/me/base-cvs': (_call, n) => jsonResponse(200, { items: n === 1 ? [] : [claimed] }),
      [`POST ${CLAIM_PATH}`]: ok(claimResult()),
    });
    function Probe(): React.JSX.Element {
      const list = useSavedBaseCvs();
      return (
        <p data-testid="probe">
          {(list.data?.items ?? []).map((cv) => cv.original_filename).join(',') || 'none'}
        </p>
      );
    }
    const user = userEvent.setup();
    renderOffer({ beside: <Probe /> });
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/base-cvs')).toHaveLength(1);
    });
    expect(await screen.findByTestId('probe')).toHaveTextContent('none');
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    await waitFor(() => {
      expect(screen.getByTestId('probe')).toHaveTextContent('claimed-jane.pdf');
    });
    expect(callsTo(fetch, 'GET', '/api/me/base-cvs')).toHaveLength(2);
  });
});

// --- AC-39: failures ---------------------------------------------------------------------------------

describe('GuestWorkOffer — failures, each distinct from "still working" (AC-39)', () => {
  it('200 with every count zero says there was nothing left to keep, and the offer goes', async () => {
    claimRoutes(
      ok(
        claimResult({
          base_cvs: 0,
          job_postings: 0,
          tailoring_runs: 0,
          export_jobs: 0,
          working_copies_dropped: 0,
        }),
      ),
    );
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByText(CLAIM_NOTHING_LEFT)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: KEEP_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: KEEP_PENDING_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Kept in your account/)).not.toBeInTheDocument();
  });

  it('429 says too many attempts, and the button is back after Retry-After', async () => {
    claimRoutes(() =>
      jsonResponse(
        429,
        { error: { code: 'rate_limited', message: 'slow down' } },
        { 'Retry-After': '1' },
      ),
    );
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    // Spelled out, never imported from claimCopy: an expectation imported from the code under
    // test moves with it (3.3 /verify, MINOR 3).
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many attempts — you can try again in 1 second.',
    );
    expect(screen.queryByRole('button', { name: KEEP_PENDING_LABEL })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: KEEP_LABEL })).toBeDisabled();
    await waitFor(
      () => {
        expect(screen.getByRole('button', { name: KEEP_LABEL })).toBeEnabled();
      },
      { timeout: 3000 },
    );
  });

  it('503 says nothing was moved and offers the button again — and trying again works', async () => {
    const fetch = claimRoutes((_call, n) =>
      n === 1
        ? jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } })
        : jsonResponse(200, claimResult()),
    );
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('alert')).toHaveTextContent(CLAIM_FAILED);
    expect(screen.queryByRole('button', { name: KEEP_PENDING_LABEL })).not.toBeInTheDocument();
    const again = screen.getByRole('button', { name: KEEP_LABEL });
    expect(again).toBeEnabled();
    await user.click(again);
    expect(await screen.findByRole('status')).toHaveTextContent('Kept in your account');
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(2);
  });

  it('a network failure reads the same as a 503, never as an endless pending state', async () => {
    claimRoutes(() => Promise.reject(new TypeError('Failed to fetch')));
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('alert')).toHaveTextContent(CLAIM_FAILED);
    expect(screen.getByRole('button', { name: KEEP_LABEL })).toBeEnabled();
  });

  it('401 not_signed_in takes 2.1’s sign-out path instead of spinning', async () => {
    claimRoutes(status(401, 'not_signed_in'));
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();
    expect(authStore.getSnapshot().status).toBe('authenticated');

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    await waitFor(() => {
      expect(authStore.getSnapshot().status).toBe('anonymous');
    });
    expect(screen.queryByRole('button', { name: KEEP_PENDING_LABEL })).not.toBeInTheDocument();
  });

  it('C-39: an expired access token is refreshed once and the claim retried', async () => {
    let attempt = 0;
    const fetch = stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      [`POST ${CLAIM_PATH}`]: () => {
        attempt += 1;
        return attempt === 1
          ? jsonResponse(401, { error: { code: 'invalid_access_token', message: 'expired' } })
          : jsonResponse(200, claimResult());
      },
      'POST /api/auth/refresh': ok({
        access_token: 'token-refreshed',
        token_type: 'Bearer',
        expires_in: 900,
        user: USER_A,
      }),
    });
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('status')).toHaveTextContent('Kept in your account');
    expect(callsTo(fetch, 'POST', '/api/auth/refresh')).toHaveLength(1);
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(2);
  });
});

// --- AC-40: Not now -----------------------------------------------------------------------------------

describe('GuestWorkOffer — Not now (AC-40)', () => {
  it('hides the offer and sends nothing; the offer returns on the next mount', async () => {
    const fetch = claimRoutes(ok(claimResult()));
    const user = userEvent.setup();
    const first = renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: NOT_NOW_LABEL }));

    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(0);
    expect(fetch.calls.filter((call) => call.method !== 'GET')).toHaveLength(0);
    first.unmount();
    renderOffer();
    expect(await findOffer()).toBeInTheDocument();
  });

  it('writes nothing to browser storage', async () => {
    claimRoutes(ok(claimResult()));
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const user = userEvent.setup();
    renderOffer();
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: NOT_NOW_LABEL }));

    expect(screen.queryByRole('region', OFFER)).not.toBeInTheDocument();
    expect(setItem).not.toHaveBeenCalled();
  });
});

// --- AC-42: the account's cache ------------------------------------------------------------------------

describe('GuestWorkOffer — the claim belongs to the account (AC-42)', () => {
  it('registers the in-flight claim under the account-rooted mutation key', async () => {
    claimRoutes(hang);
    const queryClient = newClient();
    const user = userEvent.setup();
    renderOffer({ queryClient });
    const region = await findOffer();

    await user.click(within(region).getByRole('button', { name: KEEP_LABEL }));

    await waitFor(() => {
      expect(
        queryClient.isMutating({ mutationKey: ['auth', 'account', USER_A.id, 'claimGuestWork'] }),
      ).toBe(1);
    });
  });
});
