import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import {
  ADMIN,
  TARGET_ID,
  contentRoutes,
  entry,
  entryTitled,
  posting,
  savedCv,
  summaryOf,
  world,
} from '@/test/adminContentFetch';
import { renderWithRouter } from '@/test/render';

import type { RouteHandler } from '@/test/accountFetch';
import type { ContentWorld } from '@/test/adminContentFetch';

/**
 * T12 RED — slice 4.3, AC-26: the Content area on `/admin/users/:userId`. Three sections, each with
 * its own loading sentence (spelled literally: the owner amended them on 2026-10-12), empty
 * sentence, error + Retry, and rows linking to the read pages.
 *
 * Against T11's skeleton (three empty `<section data-content>`) each fails on an absent heading.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(w: ContentWorld, overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch(contentRoutes(w, overrides));
  signInAs(ADMIN);
  return fetch;
}

const open = () =>
  renderWithRouter(`/admin/users/${TARGET_ID}`, { queryClient: createAppQueryClient() });

async function section(heading: string): Promise<HTMLElement> {
  const h = await screen.findByRole('heading', { level: 2, name: heading });
  const found = h.closest('section');
  if (found === null) throw new Error(`no <section> around "${heading}"`);
  return found;
}

const LOADING = {
  cvs: 'Loading saved CVs…',
  history: 'Loading history…',
  postings: 'Loading job postings…',
} as const;
const P = '/api/admin/users/:id';
const SLOW = { timeout: 4000 };

describe('the Content area — AC-26', () => {
  it('has three sections with their own h2 headings', async () => {
    serve(world());
    open();

    for (const heading of ['Saved CVs', 'History', 'Job postings']) {
      expect(await section(heading)).toBeInTheDocument();
    }
  });

  it('each section has its own loading sentence, a status, while its list is pending', async () => {
    serve(world(), {
      [`GET ${P}/base-cvs`]: hang,
      [`GET ${P}/tailoring-runs`]: hang,
      [`GET ${P}/job-postings`]: hang,
    });
    open();

    for (const [heading, sentence] of [
      ['Saved CVs', LOADING.cvs],
      ['History', LOADING.history],
      ['Job postings', LOADING.postings],
    ] as const) {
      const loading = within(await section(heading)).getByText(sentence);
      expect(loading).toHaveAttribute('role', 'status');
    }
  });

  it('an empty list says so, per section, and shows no loading line', async () => {
    serve(world({ cvs: [], postings: [], historyPages: [[]] }));
    open();

    expect(await within(await section('Saved CVs')).findByText('No saved CVs.')).toBeVisible();
    expect(
      await within(await section('History')).findByText('No tailoring history.'),
    ).toBeVisible();
    expect(
      await within(await section('Job postings')).findByText('No job postings.'),
    ).toBeVisible();
    expect(screen.queryByText(LOADING.cvs)).not.toBeInTheDocument();
  });

  it('a failed list is an alert with Retry, distinct from loading; Retry loads the rows', async () => {
    let calls = 0;
    const w = world();
    serve(w, {
      [`GET ${P}/base-cvs`]: () => {
        calls += 1;
        return calls <= 2
          ? status(503, 'service_unavailable')()
          : Response.json({ items: [{ ...savedCv(), extracted_text: undefined }] });
      },
    });
    open();

    const cvs = await section('Saved CVs');
    const alert = await within(cvs).findByRole('alert', {}, SLOW);
    expect(within(cvs).queryByRole('status')).not.toBeInTheDocument();
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await within(cvs).findByRole('link', { name: /Backend roles/ })).toBeVisible();
    expect(within(cvs).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a failing section leaves the other two sections working', async () => {
    serve(world(), { [`GET ${P}/job-postings`]: status(503, 'service_unavailable') });
    open();

    expect(await within(await section('Job postings')).findByRole('alert', {}, SLOW)).toBeVisible();
    expect(await within(await section('Saved CVs')).findByRole('link')).toBeVisible();
  });

  it('CV rows link to the CV page, by label (else filename)', async () => {
    serve(
      world({
        cvs: [
          savedCv({ id: 'cv-1' }),
          savedCv({ id: 'cv-2', label: null, original_filename: 'x.pdf' }),
        ],
      }),
    );
    open();

    const cvs = await section('Saved CVs');
    expect(await within(cvs).findByRole('link', { name: /Backend roles/ })).toHaveAttribute(
      'href',
      `/admin/users/${TARGET_ID}/cvs/cv-1`,
    );
    expect(within(cvs).getByRole('link', { name: /x\.pdf/ })).toHaveAttribute(
      'href',
      `/admin/users/${TARGET_ID}/cvs/cv-2`,
    );
  });

  it('history rows link to the run page by the posting title', async () => {
    serve(world());
    open();

    const link = await within(await section('History')).findByRole('link', {
      name: /Senior Platform Engineer/,
    });
    expect(link).toHaveAttribute('href', `/admin/users/${TARGET_ID}/runs/run-1`);
  });

  it('posting rows link to the posting page', async () => {
    serve(world());
    open();

    const link = await within(await section('Job postings')).findByRole('link', {
      name: /Senior Platform Engineer/,
    });
    expect(link).toHaveAttribute('href', `/admin/users/${TARGET_ID}/postings/posting-1`);
  });

  it('History shows Load more while next_cursor is set, loads the next page, then none', async () => {
    const fetch = serve(
      world({
        historyPages: [
          [entryTitled('r1', 'First page job')],
          [entryTitled('r2', 'Second page job')],
        ],
      }),
    );
    open();

    const history = await section('History');
    await within(history).findByRole('link', { name: /First page job/ });
    await userEvent.setup().click(within(history).getByRole('button', { name: 'Load more' }));

    expect(await within(history).findByRole('link', { name: /Second page job/ })).toBeVisible();
    expect(within(history).getByRole('link', { name: /First page job/ })).toBeVisible();
    expect(within(history).queryByRole('button', { name: /load more/i })).not.toBeInTheDocument();
    const paged = fetch.calls.filter((c) => c.query.get('cursor') !== null);
    expect(paged.map((c) => c.query.get('cursor'))).toEqual(['c1']);
  });

  it('Load more is disabled while the next page is fetching', async () => {
    serve(world({ historyPages: [[entry()], [entry({ id: 'r2' })]] }), {
      [`GET ${P}/tailoring-runs`]: (call) => {
        if (call.query.get('cursor') !== null) return hang();
        return Response.json({ items: [entry()], next_cursor: 'c1' });
      },
    });
    open();

    const history = await section('History');
    await userEvent
      .setup()
      .click(await within(history).findByRole('button', { name: 'Load more' }));

    await waitFor(() => {
      expect(within(history).getByRole('button', { name: /load(ing)? more/i })).toBeDisabled();
    });
  });

  it('a single page shows no Load more', async () => {
    serve(world());
    open();

    const history = await section('History');
    await within(history).findByRole('link', { name: /Senior Platform Engineer/ });
    expect(within(history).queryByRole('button', { name: /load more/i })).not.toBeInTheDocument();
  });

  it('twenty postings say "Showing the 20 most recent."; fewer do not', async () => {
    const twenty = Array.from({ length: 20 }, (_, i) => posting({ id: `p-${String(i)}` }));
    serve(world({ postings: twenty }));
    open();

    const postings = await section('Job postings');
    expect(await within(postings).findByText('Showing the 20 most recent.')).toBeVisible();
  });

  it('two postings carry no window note', async () => {
    serve(world({ postings: [posting({ id: 'a' }), posting({ id: 'b' })] }));
    open();

    const postings = await section('Job postings');
    await within(postings).findAllByRole('link');
    expect(within(postings).queryByText('Showing the 20 most recent.')).not.toBeInTheDocument();
    expect(summaryOf(posting()).preview).toBeTruthy();
  });
});
