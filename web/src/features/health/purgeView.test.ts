import { describe, expect, it } from 'vitest';

import { describeAge, viewOfGuestPurge } from './purgeView';

import type { GuestPurgeStatus } from '@/api/health';

/**
 * T36 RED — `viewOfGuestPurge`'s truth table and `describeAge`'s phrasing (AC-35).
 *
 * Written against the **spec and the skeleton's own documented branch order**, not against an
 * implementation — there is none: both functions throw. Every row below therefore fails on
 * `Error: not implemented: …` raised where a value was expected, never on an import, because the
 * module, the union and both signatures already exist (T35).
 *
 * The rows are the copy table in technical-plan.md §5 plus the two edge cases T35 decided and
 * recorded in `purgeView.ts`: a `healthy` reading with no `last_run`, and `overdue: null`.
 */

function job(overrides: Partial<GuestPurgeStatus> = {}): GuestPurgeStatus {
  return {
    scheduled: true,
    last_run: '2026-09-20T09:48:00Z',
    last_run_age_seconds: 720,
    last_outcome: 'ok',
    overdue: 0,
    stale: false,
    detail: null,
    ...overrides,
  };
}

describe('viewOfGuestPurge — the branch order is the specification', () => {
  it('undefined is `unreported` — an API older than this bundle (R-32)', () => {
    expect(viewOfGuestPurge(undefined)).toEqual({ kind: 'unreported' });
  });

  it('scheduled and not stale is `healthy`, carrying the age and the backlog', () => {
    expect(viewOfGuestPurge(job())).toEqual({
      kind: 'healthy',
      ageSeconds: 720,
      overdue: 0,
    });
  });

  it('scheduled and stale is `stale`', () => {
    expect(
      viewOfGuestPurge(job({ stale: true, last_run_age_seconds: 14_400, overdue: 37 })),
    ).toEqual({ kind: 'stale', ageSeconds: 14_400, overdue: 37 });
  });

  it('scheduled: false is `unscheduled`, and carries no age', () => {
    // No `ageSeconds` member at all: how long ago the last *manual* run was is not the fact that
    // matters when the answer is "there is no schedule".
    expect(viewOfGuestPurge(job({ scheduled: false, overdue: 10 }))).toEqual({
      kind: 'unscheduled',
      overdue: 10,
    });
  });

  /**
   * **The row that matters most, and the reason the order is written down rather than left to
   * whichever `if` came first.**
   *
   * The server guarantees `scheduled: false ⇒ stale: false` (AC-33), so this combination cannot
   * arrive from a correct API — but `viewOfGuestPurge` is total over the *wire type*, and a client
   * must not depend on a server invariant it cannot enforce (Constitution §4.5).
   *
   * If the two branches were swapped, the cost is specific and lands on exactly the deployment
   * this slice ships: `GUEST_PURGE_ENABLED` is false until the rehearsal earns the flip, so **every
   * pre-rehearsal page load would show the red "has not run for over 3 hours" alarm** instead of
   * the honest "the schedule is off". That is an alarm which is wrong for the entire window it is
   * live — the precise failure AC-33 exists to prevent on the server side, reintroduced on the
   * client.
   *
   * "Off" also wins on the merits: a job nothing schedules cannot be *behind* schedule, so the
   * staleness verdict is not merely less useful here, it is meaningless.
   */
  it('scheduled: false wins over stale: true — a job nothing schedules cannot be behind schedule', () => {
    expect(viewOfGuestPurge(job({ scheduled: false, stale: true, overdue: 10 }))).toEqual({
      kind: 'unscheduled',
      overdue: 10,
    });
  });

  it('healthy with no last_run carries ageSeconds: null rather than asserting one', () => {
    // Unreachable from a correct server (`stale` is forced true when `last_run` is null and the job
    // is scheduled) — which is exactly why a `!` here would be tempting. The failure mode of that
    // shortcut is "ran NaN minutes ago" in an operator's browser.
    expect(viewOfGuestPurge(job({ last_run: null, last_run_age_seconds: null }))).toEqual({
      kind: 'healthy',
      ageSeconds: null,
      overdue: 0,
    });
  });

  it('a stale job that has never run carries ageSeconds: null — reachable, unlike healthy', () => {
    expect(
      viewOfGuestPurge(
        job({ stale: true, last_run: null, last_run_age_seconds: null, overdue: 4 }),
      ),
    ).toEqual({ kind: 'stale', ageSeconds: null, overdue: 4 });
  });

  it.each([
    ['healthy', job({ overdue: null }), 'healthy'],
    ['stale', job({ stale: true, overdue: null }), 'stale'],
    ['unscheduled', job({ scheduled: false, overdue: null }), 'unscheduled'],
  ])(
    '%s carries overdue: null through rather than smoothing it to 0 (R-29)',
    (_name, input, kind) => {
      const view = viewOfGuestPurge(input);
      expect(view.kind).toBe(kind);
      // The count can fail while the response stays 200. A number that could not be taken must stay
      // distinguishable from zero: `overdue` is the field the runbook says to trust *over* the
      // heartbeat, so an operator who reads a confident 0 here reads it as "nothing is waiting".
      expect(view).toHaveProperty('overdue', null);
    },
  );
});

describe('describeAge — formatting, not judgement', () => {
  it.each([
    [0, /second/],
    [1, /^1 second ago$/],
    [45, /second/],
    [60, /^1 minute ago$/],
    [720, /^12 minutes ago$/],
    [3600, /^1 hour ago$/],
    [14_400, /^4 hours ago$/],
    [172_800, /^2 days ago$/],
  ])('%i seconds reads as %s', (seconds, expected) => {
    expect(describeAge(seconds)).toMatch(expected);
  });

  it('never returns a bare number and never a timestamp', () => {
    const phrase = describeAge(720);
    expect(phrase).not.toMatch(/^\d+$/);
    expect(phrase).not.toMatch(/\d{4}-\d{2}-\d{2}/);
  });
});
