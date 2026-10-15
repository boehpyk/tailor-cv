import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import {
  ADMIN,
  guestRoutes,
  guestWorld,
  PURGE_PREVIEW,
  readinessReport,
} from '@/test/adminGuestsFetch';
import { renderWithRouter } from '@/test/render';

import {
  DEPENDENCY_UNHEALTHY,
  IDENTITY_SWEEP_LABEL,
  PURGE_STALE,
  READINESS_ERROR,
  STORAGE_BUCKET_LABELS,
  STORAGE_ERROR,
  STORAGE_UNREFERENCED_NOTE,
  sweepReportSentence,
} from '../adminCopy';
import { guestListKey } from '../hooks/useGuestSessions';

import type { GuestWorld } from '@/test/adminGuestsFetch';
import type { RouteHandler } from '@/test/accountFetch';
import type { Readiness } from '@/api/health';

/**
 * T21 RED — slice 4.4, AC-36 (storage), AC-37 (purge), AC-38 (sweep) and AC-39 (job health).
 * Against T20's skeleton (empty stubs) each fails on an absent element; nothing here imports a
 * production component, so a crash would be the fixture's.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const SLOW = { timeout: 4000 };

function open(w: GuestWorld = guestWorld(), overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch(guestRoutes(w, overrides));
  signInAs(ADMIN);
  const queryClient = createAppQueryClient();
  const view = renderWithRouter('/admin/storage', { queryClient });
  return { fetch, queryClient, ...view };
}

const mainEl = (): HTMLElement => screen.getByRole('main');
const readyCalls = (fetch: ReturnType<typeof stubAccountFetch>): number =>
  callsTo(fetch, 'GET', '/health/ready').length;

async function click(name: string): Promise<void> {
  const button = await within(mainEl()).findByRole('button', { name });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

async function confirmInDialog(name: string): Promise<void> {
  const dialog = await screen.findByRole('alertdialog');
  fireEvent.click(within(dialog).getByRole('button', { name }));
}

describe('storage — AC-36', () => {
  it('loading: a status line while the volume is measured', async () => {
    const { fetch } = open(guestWorld(), { 'GET /api/admin/storage': hang });

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/admin/storage')).toHaveLength(1);
    });
    expect(within(mainEl()).getByText('Measuring storage…')).toHaveAttribute('role', 'status');
  });

  it('a 503 is an alert with Retry; Retry shows the table', async () => {
    let calls = 0;
    const w = guestWorld();
    open(w, {
      'GET /api/admin/storage': () => {
        calls += 1;
        return calls <= 2 ? status(503, 'storage_unavailable')() : Response.json(w.storage);
      },
    });

    const alert = await within(mainEl()).findByText(STORAGE_ERROR, {}, SLOW);
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('table')).toBeVisible();
  });

  it('a table of the five buckets with files and a size unit, and a measured-at time', async () => {
    open();

    const table = await screen.findByRole('table');
    const rows = within(table).getAllByRole('row');
    const row = (label: string): HTMLElement => {
      const found = rows.find((r) => r.textContent.includes(label));
      if (found === undefined) throw new Error(`no ${label} row`);
      return found;
    };
    for (const label of Object.values(STORAGE_BUCKET_LABELS)) {
      expect(row(label)).toBeInTheDocument();
    }
    expect(row(STORAGE_BUCKET_LABELS.guest)).toHaveTextContent('12');
    expect(row(STORAGE_BUCKET_LABELS.guest)).toHaveTextContent(/GB/);
    expect(row(STORAGE_BUCKET_LABELS.user)).toHaveTextContent('31');
    expect(row(STORAGE_BUCKET_LABELS.user)).toHaveTextContent(/MB/);
    expect(row(STORAGE_BUCKET_LABELS.unreferenced)).toHaveTextContent('7');
    expect(row(STORAGE_BUCKET_LABELS.unreferenced)).toHaveTextContent(/kB/);
    expect(mainEl()).toHaveTextContent(/Measured at \d{1,2}:\d{2}/);
    expect(within(mainEl()).getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('says the orphan sweep reclaims unreferenced files past its age floor', async () => {
    open();

    await screen.findByRole('table');
    expect(screen.getByText(STORAGE_UNREFERENCED_NOTE)).toBeVisible();
  });

  it('Measure again measures again, and is disabled while fetching', async () => {
    let calls = 0;
    const w = guestWorld();
    const { fetch } = open(w, {
      'GET /api/admin/storage': () => {
        calls += 1;
        return calls === 1 ? Response.json(w.storage) : hang();
      },
    });

    await screen.findByRole('table');
    const again = screen.getByRole('button', { name: 'Measure again' });
    await waitFor(() => {
      expect(again).toBeEnabled();
    });
    fireEvent.click(again);

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/admin/storage')).toHaveLength(2);
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Measure again' })).toBeDisabled();
    });
  });
});

describe('the purge panel — AC-37', () => {
  it('the preview fake has a real dry-run shape (AC-16): nothing deleted, examined is the count', () => {
    expect(PURGE_PREVIEW.dry_run).toBe(true);
    expect(PURGE_PREVIEW.sessions_deleted).toBe(0);
    expect(PURGE_PREVIEW.examined).toBeGreaterThan(0);
  });

  it('shows the job facts: scheduled, last run and outcome, overdue', async () => {
    open();

    expect(await within(mainEl()).findByText('Scheduled')).toBeVisible();
    expect(mainEl()).toHaveTextContent(/Last run .+ ago: ok/);
    expect(within(mainEl()).getByText('Overdue: 4')).toBeVisible();
    expect(within(mainEl()).queryByText(PURGE_STALE)).not.toBeInTheDocument();
  });

  it('not scheduled says guest data is deleted only when someone runs it', async () => {
    const report = readinessReport();
    open(
      guestWorld({
        readiness: {
          ...report,
          jobs: {
            ...report.jobs,
            guest_purge: {
              scheduled: false,
              last_run: null,
              last_run_age_seconds: null,
              last_outcome: null,
              overdue: 4,
              stale: false,
              detail: null,
            },
          },
        },
      }),
    );

    expect(
      await within(mainEl()).findByText(
        'Not scheduled — guest data is deleted only when someone runs it',
      ),
    ).toBeVisible();
  });

  it('a stale heartbeat shows the stale warning', async () => {
    const report = readinessReport();
    const purge = report.jobs?.guest_purge;
    if (purge === undefined) throw new Error('fixture has no purge');
    open(
      guestWorld({
        readiness: { ...report, jobs: { ...report.jobs, guest_purge: { ...purge, stale: true } } },
      }),
    );

    expect(await within(mainEl()).findByText(PURGE_STALE)).toBeVisible();
  });

  it('Preview purge reads the preview and says nothing was deleted; no POST', async () => {
    const { fetch } = open();

    await click('Preview purge');

    expect(
      await screen.findByText('Would delete 3 session(s) and 7 file(s). Nothing was deleted.'),
    ).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/admin/guest-purge')).toHaveLength(1);
    expect(callsTo(fetch, 'POST', '/api/admin/guest-purge')).toHaveLength(0);
  });

  it('Run purge confirms first, naming the preview’s numbers; Cancel sends nothing', async () => {
    const { fetch } = open();

    await click('Preview purge');
    await screen.findByText(/Would delete 3 session/);
    await click('Run purge');
    const dialog = await screen.findByRole('alertdialog');

    expect(dialog).toHaveTextContent(/\b3\b/);
    expect(dialog).toHaveTextContent(/\b7\b/);
    await userEvent.setup().click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(callsTo(fetch, 'POST', '/api/admin/guest-purge')).toHaveLength(0);
  });

  it('without a preview the dialog says "up to 100"', async () => {
    open();

    await click('Run purge');

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(/up to 100/i);
  });

  it('confirm posts once and shows the report with failures as counts', async () => {
    const { fetch } = open();

    await click('Run purge');
    await confirmInDialog('Run purge');

    const report = await screen.findByText(/Deleted 3 session\(s\), 7 file\(s\)/);
    expect(report).toHaveTextContent(/9 still overdue/);
    expect(report).toHaveTextContent(/1 session\(s\)/);
    expect(report).toHaveTextContent(/2 file\(s\)/);
    expect(callsTo(fetch, 'POST', '/api/admin/guest-purge')).toHaveLength(1);
  });

  it('409 purge_in_progress is never worded as success', async () => {
    open(guestWorld(), { 'POST /api/admin/guest-purge': status(409, 'purge_in_progress') });

    await click('Run purge');
    await confirmInDialog('Run purge');

    expect(
      await screen.findByText('A purge is already running. Nothing was deleted by this request.'),
    ).toBeVisible();
    expect(screen.queryByText(/Deleted \d+ session/)).not.toBeInTheDocument();
  });

  it('503 purge_failed says sessions already deleted stay deleted', async () => {
    open(guestWorld(), { 'POST /api/admin/guest-purge': status(503, 'purge_failed') });

    await click('Run purge');
    await confirmInDialog('Run purge');

    expect(
      await screen.findByText('The purge failed. Sessions already deleted stay deleted.'),
    ).toBeVisible();
    expect(screen.queryByText(/Deleted \d+ session/)).not.toBeInTheDocument();
  });

  it('after a run the readiness, storage and guest-list queries are invalidated', async () => {
    const { fetch, queryClient } = open();
    await screen.findByRole('table');
    await queryClient.infiniteQuery({
      queryKey: guestListKey(ADMIN.id),
      queryFn: () => ({ items: [], next_cursor: null }),
      initialPageParam: null as string | null,
    });
    await waitFor(() => {
      expect(readyCalls(fetch)).toBeGreaterThan(0);
    });
    const readyBefore = readyCalls(fetch);
    const storageBefore = callsTo(fetch, 'GET', '/api/admin/storage').length;

    await click('Run purge');
    await confirmInDialog('Run purge');
    await screen.findByText(/Deleted 3 session/);

    await waitFor(() => {
      expect(readyCalls(fetch)).toBeGreaterThan(readyBefore);
    });
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/admin/storage').length).toBeGreaterThan(storageBefore);
    });
    expect(queryClient.getQueryState(guestListKey(ADMIN.id))?.isInvalidated).toBe(true);
  });

  it('a double click on confirm posts once', async () => {
    const { fetch } = open();

    await click('Run purge');
    const dialog = await screen.findByRole('alertdialog');
    const confirm = within(dialog).getByRole('button', { name: 'Run purge' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await screen.findByText(/Deleted 3 session/);
    expect(callsTo(fetch, 'POST', '/api/admin/guest-purge')).toHaveLength(1);
  });
});

describe('the sweep panel — AC-38', () => {
  it('Preview sweep says what it would reclaim and that nothing was deleted; no POST', async () => {
    const { fetch } = open();

    await click('Preview sweep');

    expect(
      await screen.findByText(
        'Would reclaim 5 file(s); 6 referenced, 2 too young, 1 unrecognized. Nothing was deleted.',
      ),
    ).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/admin/orphan-sweep')).toHaveLength(1);
    expect(callsTo(fetch, 'POST', '/api/admin/orphan-sweep')).toHaveLength(0);
  });

  it('Run sweep confirms, posts once and shows the report', async () => {
    const { fetch } = open();

    await click('Run sweep');
    await confirmInDialog('Run sweep');

    expect(await screen.findByText(sweepReportSentence({ reclaimed: 5, failed: 0 }))).toBeVisible();
    expect(callsTo(fetch, 'POST', '/api/admin/orphan-sweep')).toHaveLength(1);
  });

  it('503 orphan_check_unavailable says the check could not run, so nothing was deleted', async () => {
    open(guestWorld(), {
      'POST /api/admin/orphan-sweep': status(503, 'orphan_check_unavailable'),
    });

    await click('Run sweep');
    await confirmInDialog('Run sweep');

    expect(
      await screen.findByText('The database check could not run, so nothing was deleted.'),
    ).toBeVisible();
  });
});

describe('job health — AC-39', () => {
  it('names each dependency, healthy or not, and the identity sweep’s overdue', async () => {
    const base = readinessReport();
    open(
      guestWorld({
        readiness: {
          ...base,
          ready: false,
          dependencies: { ...base.dependencies, redis: { healthy: false, detail: 'down' } },
        },
        readinessStatus: 503,
      }),
    );

    for (const label of ['Database', 'Redis', 'Worker']) {
      expect(await within(mainEl()).findByText(label)).toBeVisible();
    }
    expect(within(mainEl()).getAllByText('Healthy')).toHaveLength(2);
    expect(within(mainEl()).getAllByText(DEPENDENCY_UNHEALTHY)).toHaveLength(1);
    expect(within(mainEl()).getByText(IDENTITY_SWEEP_LABEL)).toBeVisible();
    expect(within(mainEl()).getByText('Overdue: 2')).toBeVisible();
  });

  it('jobs absent (an older API) says "Not reported by this API."', async () => {
    const withoutJobs: Readiness = { ready: true, dependencies: readinessReport().dependencies };
    open(guestWorld({ readiness: withoutJobs }));

    expect(await within(mainEl()).findByText('Not reported by this API.')).toBeVisible();
  });

  it('readiness failing is an alert, and the rest of the page stays usable', async () => {
    const { fetch } = open(guestWorld({ readiness: null }));

    const alert = await within(mainEl()).findByText(READINESS_ERROR);
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    expect(await screen.findByRole('table')).toBeVisible();
    await click('Preview purge');
    expect(await screen.findByText(/Would delete 3 session/)).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/admin/guest-purge')).toHaveLength(1);
  });
});
