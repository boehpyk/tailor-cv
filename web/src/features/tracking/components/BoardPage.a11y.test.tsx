import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, hang, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  APPLICATIONS_PATH,
  BOARD_PATH,
  apiError,
  boardServer,
  cardWithText,
  makeCard,
  moveControlOf,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T29 (`qa`, test-after) — AC-40: accessibility and link safety of the board.
 *
 * Every assertion below was observed red under a named mutation of production code, restored
 * byte-exact (`git diff --stat -- web/src` empty afterwards). Mutation → red is written beside each
 * test as `MUT:`.
 */

const TITLE = 'Platform role at Acme';
const STAGES_IN_ORDER = ['To apply', 'Applied', 'Interviewing', 'Offer', 'Rejected', 'Withdrawn'];

function serve(cards: readonly BoardCard[], overrides: Record<string, RouteHandler> = {}) {
  const server = boardServer({ 'user-a': cards });
  stubAccountFetch({ ...server.routes(), ...overrides });
  return server;
}

function withPostingUrl(source_url: string | null): BoardCard {
  const card = makeCard();
  return {
    ...card,
    posting: card.posting === null ? null : { ...card.posting, source_url },
  };
}

/** The text of the elements an `aria-describedby` points at, in order. */
function descriptionOf(element: HTMLElement): string {
  return (element.getAttribute('aria-describedby') ?? '')
    .split(/\s+/)
    .filter((id) => id !== '')
    .map((id) => document.getElementById(id)?.textContent ?? '')
    .join(' ');
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('AC-40 — columns are labelled regions in board order', () => {
  it('exposes exactly six regions, named for their stage with a count, in STAGES order', async () => {
    serve([makeCard()]);
    renderBoard();
    await screen.findByText(TITLE);

    const regions = screen.getAllByRole('region');

    // MUT: BoardColumn.tsx — delete `aria-labelledby={headingId}` → names are '' → red.
    // MUT: BoardPage.tsx — the outer `div` gained `role="region" aria-label=…` → seven regions → red.
    expect(regions.map((region) => region.getAttribute('aria-labelledby') ?? '')).not.toContain('');
    expect(regions).toHaveLength(6);
    STAGES_IN_ORDER.forEach((label, index) => {
      const count = label === 'To apply' ? 1 : 0;
      expect(regions[index]).toHaveAccessibleName(`${label} (${String(count)})`);
    });
  });
});

describe('AC-40 — every control is labelled, and says which card it belongs to', () => {
  it('Move to is a native <select> with an accessible name, described by the card title', async () => {
    serve([makeCard()]);
    renderBoard();
    await screen.findByText(TITLE);

    const move = moveControlOf(cardWithText(TITLE));

    // MUT: MoveToControl.tsx — `<select>` → `<div role="combobox" tabIndex={0}>` (both tags) → tagName red.
    // MUT: MoveToControl.tsx — delete `aria-label={MOVE_TO_LABEL}` → no combobox named "Move to" → red.
    // MUT: BoardCard.tsx — delete `describedBy={titleId}` on `MoveToControl` → description '' → red.
    expect(move.tagName).toBe('SELECT');
    expect(move).toHaveAccessibleName('Move to');
    expect(descriptionOf(move)).toBe(TITLE);
  });

  it('Edit title and Remove from board are described by the same card title', async () => {
    serve([makeCard()]);
    renderBoard();
    await screen.findByText(TITLE);
    const card = cardWithText(TITLE);

    // MUT: BoardCard.tsx — delete `describedBy={titleId}` on `CardTitleEditor` → red (first).
    // MUT: BoardCard.tsx — Remove's `aria-describedby={…titleId}` → undefined → red (second).
    expect(descriptionOf(within(card).getByRole('button', { name: 'Edit title' }))).toBe(TITLE);
    expect(descriptionOf(within(card).getByRole('button', { name: 'Remove from board' }))).toBe(
      TITLE,
    );
  });

  it('the title input is labelled, and its counter is linked by aria-describedby', async () => {
    serve([makeCard()]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);
    await user.click(within(cardWithText(TITLE)).getByRole('button', { name: 'Edit title' }));

    const input = screen.getByRole('textbox', { name: 'Title' });

    // MUT: CardTitleEditor.tsx — `aria-describedby={failure === null ? counterId : …}` → undefined
    // when there is no failure → description '' → red.
    expect(descriptionOf(input)).toBe(`${String(TITLE.length)} / 120`);
    expect(input).toHaveAttribute('aria-invalid', 'false');
  });

  it('a refused title: aria-invalid, and the alert is linked alongside the counter', async () => {
    serve([makeCard()], {
      [`PUT ${APPLICATIONS_PATH}/:id/title`]: () =>
        jsonResponse(422, { error: { code: 'validation_error', message: 'Too fancy' } }),
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);
    await user.click(within(cardWithText(TITLE)).getByRole('button', { name: 'Edit title' }));
    await user.type(screen.getByRole('textbox', { name: 'Title' }), '!{Enter}');

    const alert = await screen.findByRole('alert');
    const input = screen.getByRole('textbox', { name: 'Title' });

    // MUT: CardTitleEditor.tsx — `aria-invalid={failure !== null}` → `{false}` → red (first).
    // MUT: CardTitleEditor.tsx — drop `${errorId}` from the describedby → red (second).
    expect(alert).toHaveTextContent('Too fancy');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(descriptionOf(input)).toContain('Too fancy');
    expect(descriptionOf(input)).toContain('/ 120');
  });

  it('a refused removal: the alert is linked to the Remove button, after the card title', async () => {
    serve([makeCard()], {
      [`DELETE ${APPLICATIONS_PATH}/:id`]: () => apiError(503, 'service_unavailable'),
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);
    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: 'Remove from board' }),
    );

    const alert = await screen.findByText('Not removed — try again.');
    const remove = within(cardWithText(TITLE)).getByRole('button', { name: 'Remove from board' });

    // MUT: BoardCard.tsx — Remove's `aria-describedby` → `titleId` only → description lacks the note → red.
    expect(alert).toHaveAttribute('role', 'alert');
    expect(descriptionOf(remove)).toBe(`${TITLE} Not removed — try again.`);
  });
});

describe('AC-40 — the polite live region exists in every state', () => {
  const states: readonly [string, () => void][] = [
    [
      'loading',
      () => stubAccountFetch({ ...boardServer({}).routes(), [`GET ${BOARD_PATH}`]: hang }),
    ],
    [
      'error',
      () =>
        stubAccountFetch({
          ...boardServer({}).routes(),
          [`GET ${BOARD_PATH}`]: () => apiError(503, 'service_unavailable'),
        }),
    ],
    ['empty', () => serve([])],
    ['success', () => serve([makeCard()])],
  ];

  it.each(states)('%s: an aria-live="polite" atomic region is mounted', async (name, arrange) => {
    arrange();
    const { container } = renderBoard();
    if (name === 'success') {
      await screen.findByText(TITLE);
    } else if (name === 'empty') {
      await screen.findByText('Nothing on your board yet');
    } else if (name === 'error') {
      await screen.findByText("Couldn't load your board");
    } else {
      await screen.findByText('Loading your board…');
    }

    // MUT: BoardPage.tsx — `aria-live="polite"` → `aria-live="off"` → red in all four states.
    const live = container.querySelector('[aria-live="polite"]');
    expect(live).not.toBeNull();
    expect(live).toHaveAttribute('aria-atomic', 'true');
  });
});

describe('AC-40 — the posting link renders only for http(s)', () => {
  it('http and https (any case) render one external link with rel="noopener noreferrer"', async () => {
    serve([withPostingUrl('https://jobs.example.com/p/1')]);
    renderBoard();
    await screen.findByText(TITLE);

    const link = within(cardWithText(TITLE)).getByRole('link', { name: 'Job posting' });

    // MUT: BoardCard.tsx — `rel="noopener noreferrer"` → `rel="noopener"` → red (rel).
    expect(link).toHaveAttribute('href', 'https://jobs.example.com/p/1');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it.each(['http://jobs.example.com/p/1', 'HTTPS://JOBS.EXAMPLE.COM/p/1'])(
    '%s is linked',
    async (url) => {
      serve([withPostingUrl(url)]);
      renderBoard();
      await screen.findByText(TITLE);

      expect(
        within(cardWithText(TITLE)).getByRole('link', { name: 'Job posting' }),
      ).toBeInTheDocument();
    },
  );

  it.each([
    'javascript:alert(document.cookie)',
    'JaVaScRiPt:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'ftp://files.example.com/p',
    '//evil.example/p',
    ' https://leading-space.example/p',
    'not a url',
    '',
  ])('%j renders no link at all', async (url) => {
    serve([withPostingUrl(url)]);
    renderBoard();
    await screen.findByText(TITLE);
    const card = cardWithText(TITLE);

    // MUT: BoardCard.tsx `safePostingUrl` — `return url !== null ? url : null` (guard removed) → red
    // for every row; and `/^https?:\/\//i` → `/^(https?|javascript):/i` → red for the javascript rows.
    expect(within(card).queryByRole('link', { name: 'Job posting' })).not.toBeInTheDocument();
    expect(card.querySelector('a[href]:not([href^="/"])')).toBeNull();
    // Positive control: the same card still has its own internal link, so "no link" is not "no card".
    expect(within(card).getByRole('link', { name: 'Open tailored CV' })).toBeInTheDocument();
  });
});

describe('AC-40 — at 360 px the columns stack (the classes that produce it)', () => {
  /** jsdom computes no layout, so this asserts the Tailwind structure: the base (unprefixed) rule. */
  function unprefixed(element: Element): string[] {
    return element.className.split(/\s+/).filter((token) => token !== '' && !token.includes(':'));
  }

  it('the six columns share one container whose unprefixed classes are a vertical grid, with no sideways scroll or fixed width below lg', async () => {
    serve([makeCard()]);
    renderBoard();
    await screen.findByText(TITLE);

    const regions = screen.getAllByRole('region');
    const container = regions[0]?.parentElement;
    if (container === null || container === undefined) {
      throw new Error('regions have no parent');
    }
    const base = unprefixed(container);

    // MUT: BoardPage.tsx — `grid gap-3 sm:grid-cols-2 lg:flex lg:overflow-x-auto` →
    // `flex overflow-x-auto` (the row at every width) → `grid` missing, `flex` present → red.
    // MUT: BoardPage.tsx — `grid-cols-2` unprefixed → two columns at 360 px → red.
    expect(regions.every((region) => region.parentElement === container)).toBe(true);
    expect(base).toContain('grid');
    expect(base).not.toContain('flex');
    expect(base.filter((token) => /^grid-cols-|^overflow-x-|^overflow-/.test(token))).toEqual([]);
    expect(container.className).toContain('sm:grid-cols-2');
    expect(container.className).toContain('lg:overflow-x-auto');
  });

  it('a column has no unprefixed width or shrink rule, so it takes the full stacked width', async () => {
    serve([makeCard()]);
    renderBoard();
    await screen.findByText(TITLE);

    // MUT: BoardColumn.tsx — `lg:w-60 lg:shrink-0` → `w-60 shrink-0` → a 15 rem column at 360 px → red.
    for (const region of screen.getAllByRole('region')) {
      expect(
        unprefixed(region).filter((token) => /^(?:min-|max-)?w-|^shrink-/.test(token)),
      ).toEqual([]);
      expect(region.className).toContain('lg:w-60');
    }
  });
});
