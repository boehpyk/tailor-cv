import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { USER_A, callsTo, hang, ok, signInAs, status, stubAccountFetch } from '@/test/accountFetch';

import {
  DEMAND_CAPTION,
  DEMAND_COUNTER_LABELS,
  DEMAND_EMPTY,
  DEMAND_ERROR,
  DEMAND_FROM_LABEL,
  DEMAND_HEADING,
  DEMAND_LOADING,
  DEMAND_TO_LABEL,
  DEMAND_TOTALS_ROW,
} from '../adminCopy';
import { DemandPage } from './DemandPage';

import type { DemandCounters, DemandCounts } from '@/api/admin';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T33 RED (slice 5.2; AC-39, ADR-0043): the page through a fake `GET /api/admin/demand-counters`
 * with the real shape (every day in the range, zeros filled, plus totals). The range comes from the
 * URL; every counter has its own number, so a count wired to the wrong label fails.
 */

const ZERO: DemandCounts = {
  fit_scores: 0,
  fit_scores_busy: 0,
  sign_ups: 0,
  pack_clicks: 0,
  pack_clickers: 0,
  runs_failed: 0,
  runs_succeeded: 0,
};

const DAY1: DemandCounts = {
  fit_scores: 11,
  fit_scores_busy: 12,
  sign_ups: 13,
  pack_clicks: 14,
  pack_clickers: 15,
  runs_failed: 16,
  runs_succeeded: 17,
};

function counters(days: readonly [string, DemandCounts][], totals: DemandCounts): DemandCounters {
  return {
    from: '2026-11-01',
    to: '2026-11-02',
    days: days.map(([day, counts]) => ({ day, counts })),
    totals,
  };
}

function open(table: Record<string, RouteHandler>) {
  const fetch = stubAccountFetch(table);
  signInAs(USER_A);
  render(
    <QueryClientProvider client={createAppQueryClient()}>
      <MemoryRouter initialEntries={['/admin/demand?from=2026-11-01&to=2026-11-02']}>
        <DemandPage actorId={USER_A.id} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetch;
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DemandPage', () => {
  it('has one h1 and From and To date inputs holding the URL range', async () => {
    open({ 'GET /api/admin/demand-counters': hang });
    expect(await screen.findByRole('heading', { level: 1, name: DEMAND_HEADING })).toBeVisible();
    expect(screen.getByLabelText(DEMAND_FROM_LABEL)).toHaveValue('2026-11-01');
    expect(screen.getByLabelText(DEMAND_TO_LABEL)).toHaveValue('2026-11-02');
    expect(screen.getByLabelText(DEMAND_FROM_LABEL)).toHaveAttribute('type', 'date');
  });

  it('asks the server for the URL range', async () => {
    const fetch = open({
      'GET /api/admin/demand-counters': ok(counters([['2026-11-01', DAY1]], DAY1)),
    });
    await screen.findByRole('table');
    const [call] = callsTo(fetch, 'GET', '/api/admin/demand-counters');
    expect(call?.query.get('from')).toBe('2026-11-01');
    expect(call?.query.get('to')).toBe('2026-11-02');
  });

  it('says it is loading while the read is in flight', async () => {
    open({ 'GET /api/admin/demand-counters': hang });
    expect(await screen.findByText(DEMAND_LOADING)).toBeVisible();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('says it could not load, and shows no empty message, when the read fails', async () => {
    open({ 'GET /api/admin/demand-counters': status(503, 'service_unavailable') });
    expect(await screen.findByText(DEMAND_ERROR, undefined, { timeout: 9000 })).toBeVisible();
    expect(screen.queryByText(DEMAND_EMPTY)).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  }, 12000);

  it('says nothing was counted, and shows no table, when every count is zero', async () => {
    open({
      'GET /api/admin/demand-counters': ok(
        counters(
          [
            ['2026-11-01', ZERO],
            ['2026-11-02', ZERO],
          ],
          ZERO,
        ),
      ),
    });
    expect(await screen.findByText(DEMAND_EMPTY)).toBeVisible();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText(DEMAND_ERROR)).not.toBeInTheDocument();
  });

  it('shows a captioned table, a row per day and a totals row with every counter under its label', async () => {
    const day2: DemandCounts = { ...ZERO, sign_ups: 2 };
    const totals: DemandCounts = { ...DAY1, sign_ups: 15 };
    open({
      'GET /api/admin/demand-counters': ok(
        counters(
          [
            ['2026-11-01', DAY1],
            ['2026-11-02', day2],
          ],
          totals,
        ),
      ),
    });
    const table = await screen.findByRole('table', { name: DEMAND_CAPTION });
    const rows = within(table).getAllByRole('row');
    const body = rows.slice(1);
    expect(body).toHaveLength(3);
    expect(within(body[0] as HTMLElement).getByText('2026-11-01')).toBeVisible();
    expect(within(body[1] as HTMLElement).getByText('2026-11-02')).toBeVisible();
    expect(within(body[2] as HTMLElement).getByText(DEMAND_TOTALS_ROW)).toBeVisible();
    for (const label of Object.values(DEMAND_COUNTER_LABELS)) {
      expect(within(table).getByRole('columnheader', { name: label })).toBeVisible();
    }
    const column = (key: keyof DemandCounts): number =>
      Object.keys(DEMAND_COUNTER_LABELS).indexOf(key) + 1;
    const cell = (row: HTMLElement, key: keyof DemandCounts): string | null =>
      within(row).getAllByRole('cell')[column(key) - 1]?.textContent ?? null;
    expect(cell(body[0] as HTMLElement, 'pack_clickers')).toBe('15');
    expect(cell(body[0] as HTMLElement, 'fit_scores_busy')).toBe('12');
    expect(cell(body[2] as HTMLElement, 'sign_ups')).toBe('15');
  });
});
