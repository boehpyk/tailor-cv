import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, bearerFor, callsTo, hang, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { STAGES } from '../types';
import {
  BOARD_PATH,
  apiError,
  boardServer,
  cardWithText,
  makeCard,
  moveControlOf,
  otherCard,
  regionOf,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';

/**
 * T27 RED — AC-32: the board page's four states, six labelled regions with counts, and what a card
 * shows. Mounted directly (`BoardPage`, account scope), every request recorded.
 *
 * The skeleton renders a placeholder `div` for every state, so each assertion here is red on the
 * missing behaviour. The wording asserted is the spec's (AC-32 / T-38); the one constant imported
 * is where the spec names a sentence it does not quote.
 */

const STAGE_LABELS = ['To apply', 'Applied', 'Interviewing', 'Offer', 'Rejected', 'Withdrawn'];

function serve(cards: readonly BoardCard[]) {
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch(server.routes());
  return { server, fetch };
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('BoardPage — the four states (AC-32, T-38)', () => {
  it('loading: "Loading your board…" as a status, with no region yet', async () => {
    stubAccountFetch({ ...boardServer({}).routes(), [`GET ${BOARD_PATH}`]: hang });

    renderBoard();

    expect(await screen.findByText('Loading your board…')).toHaveAttribute('role', 'status');
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByText('Nothing on your board yet')).not.toBeInTheDocument();
  });

  it('error: "Couldn\'t load your board" as an alert with Retry — never the empty state; Retry asks again', async () => {
    const server = boardServer({ 'user-a': [makeCard()] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [`GET ${BOARD_PATH}`]: (call, n) =>
        n === 1 ? apiError(503, 'service_unavailable') : server.getBoard(call, n),
    });
    const user = userEvent.setup();

    renderBoard();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load your board");
    expect(screen.queryByText('Nothing on your board yet')).not.toBeInTheDocument();
    expect(screen.queryByText('Loading your board…')).not.toBeInTheDocument();
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Platform role at Acme')).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(2);
  });

  it('empty: "Nothing on your board yet" with a link to /history, and no column', async () => {
    serve([]);

    renderBoard();

    expect(await screen.findByText('Nothing on your board yet')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Add tailored applications from your history' });
    expect(link).toHaveAttribute('href', '/history');
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
  });

  it('reads the board once, with the bearer', async () => {
    const { fetch } = serve([makeCard()]);

    renderBoard();
    await screen.findByText('Platform role at Acme');

    const reads = callsTo(fetch, 'GET', BOARD_PATH);
    expect(reads).toHaveLength(1);
    expect(reads[0]?.authorization).toBe(bearerFor(USER_A));
  });
});

describe('BoardPage — six labelled columns (AC-32)', () => {
  it('shows six regions in board order, each with its count in the heading', async () => {
    serve([
      otherCard(1, { stage: 'to_apply' }),
      otherCard(2, { stage: 'to_apply' }),
      otherCard(3, { stage: 'applied' }),
    ]);

    renderBoard();
    await screen.findByText('Card number 1');

    const regions = screen.getAllByRole('region');
    expect(regions).toHaveLength(STAGES.length);
    regions.forEach((region, index) => {
      expect(region.getAttribute('aria-labelledby')).not.toBeNull();
      expect(region).toHaveAccessibleName(new RegExp(`^${STAGE_LABELS[index] ?? ''}\\b`));
    });
    expect(regionOf('To apply')).toHaveAccessibleName(/\b2\b/);
    expect(regionOf('Applied')).toHaveAccessibleName(/\b1\b/);
    expect(regionOf('Interviewing')).toHaveAccessibleName(/\b0\b/);
  });

  it('puts each card in the column of its stage', async () => {
    serve([
      otherCard(1, { stage: 'to_apply' }),
      otherCard(2, { stage: 'offer' }),
      otherCard(3, { stage: 'withdrawn' }),
    ]);

    renderBoard();
    await screen.findByText('Card number 1');

    expect(within(regionOf('To apply')).getByText('Card number 1')).toBeInTheDocument();
    expect(within(regionOf('Offer')).getByText('Card number 2')).toBeInTheDocument();
    expect(within(regionOf('Withdrawn')).getByText('Card number 3')).toBeInTheDocument();
    expect(within(regionOf('To apply')).queryByText('Card number 2')).not.toBeInTheDocument();
  });
});

describe('BoardPage — a card (AC-32)', () => {
  it('shows the title, the CV label, "since {date}", the run link and the three controls', async () => {
    serve([makeCard({ tailoring_run_id: 'run-9' })]);

    renderBoard();
    await screen.findByText('Platform role at Acme');

    const card = cardWithText('Platform role at Acme');
    expect(within(card).getByText('Backend roles')).toBeInTheDocument();
    expect(within(card).getByText(/since/i)).toBeInTheDocument();
    const open = within(card)
      .getAllByRole('link')
      .find((link) => link.getAttribute('href') === '/history/run-9/cv');
    expect(open).toBeDefined();
    expect(moveControlOf(card)).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /edit title/i })).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /remove from board/i })).toBeInTheDocument();
  });

  it('builds the run link from the account scope — /history/{id}/cv, never /runs/', async () => {
    serve([makeCard({ tailoring_run_id: 'run-7' })]);

    renderBoard();
    await screen.findByText('Platform role at Acme');

    const hrefs = within(cardWithText('Platform role at Acme'))
      .getAllByRole('link')
      .map((link) => link.getAttribute('href'));
    expect(hrefs).toContain('/history/run-7/cv');
    expect(hrefs.some((href) => href?.startsWith('/runs/'))).toBe(false);
  });

  it('title falls back to the posting title, then to the preview', async () => {
    serve([
      otherCard(1, { title: null }),
      otherCard(2, {
        title: null,
        posting: {
          job_posting_id: 'p2',
          source: 'pasted',
          title: null,
          source_url: null,
          preview: 'Only a preview survives here',
        },
      }),
    ]);

    renderBoard();

    expect(await screen.findByText('Senior Platform Engineer')).toBeInTheDocument();
    expect(screen.getByText('Only a preview survives here')).toBeInTheDocument();
    expect(screen.queryByText('Card number 1')).not.toBeInTheDocument();
  });

  it('CV line falls back to the filename, then to "CV deleted"', async () => {
    serve([
      otherCard(1, {
        base_cv: { base_cv_id: 'c', label: null, original_filename: 'plain-name.pdf' },
      }),
      otherCard(2, { base_cv: null }),
    ]);

    renderBoard();
    await screen.findByText('Card number 1');

    expect(within(cardWithText('Card number 1')).getByText('plain-name.pdf')).toBeInTheDocument();
    expect(within(cardWithText('Card number 1')).queryByText('CV deleted')).not.toBeInTheDocument();
    expect(within(cardWithText('Card number 2')).getByText('CV deleted')).toBeInTheDocument();
  });

  it('a posting URL is an external link that opens safely; no URL, no link', async () => {
    serve([
      otherCard(1, {
        posting: {
          job_posting_id: 'p1',
          source: 'fetched',
          title: 'Has a link',
          source_url: 'https://jobs.example.com/platform',
          preview: 'x',
        },
      }),
      otherCard(2),
    ]);

    renderBoard();
    await screen.findByText('Card number 1');

    const external = within(cardWithText('Card number 1'))
      .getAllByRole('link')
      .find((link) => link.getAttribute('href') === 'https://jobs.example.com/platform');
    expect(external).toHaveAttribute('target', '_blank');
    expect(external?.getAttribute('rel')?.split(/\s+/)).toEqual(
      expect.arrayContaining(['noopener', 'noreferrer']),
    );
    const noUrl = within(cardWithText('Card number 2')).getAllByRole('link');
    expect(noUrl.some((link) => link.getAttribute('target') === '_blank')).toBe(false);
  });

  it('a card whose run and posting are missing is still listed and removable', async () => {
    serve([otherCard(1, { title: null, run: null, posting: null, base_cv: null })]);

    renderBoard();

    await waitFor(() => {
      expect(screen.getAllByRole('region')).toHaveLength(6);
    });
    const removes = screen.getAllByRole('button', { name: /remove from board/i });
    expect(removes).toHaveLength(1);
    expect(within(regionOf('To apply')).getByText('CV deleted')).toBeInTheDocument();
  });

  it("the Move to control lists the five other stages and not the card's own", async () => {
    serve([makeCard({ stage: 'applied' })]);

    renderBoard();
    await screen.findByText('Platform role at Acme');

    const control = moveControlOf(cardWithText('Platform role at Acme'));
    expect(control.tagName).toBe('SELECT');
    const offered = within(control)
      .getAllByRole('option')
      .map((option) => option.textContent)
      .filter((text) => STAGE_LABELS.includes(text));
    expect(offered).toEqual(STAGE_LABELS.filter((label) => label !== 'Applied'));
  });
});
