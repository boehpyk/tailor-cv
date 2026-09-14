import { screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithQuery } from '@/test/render';

import { routes } from './router';

/**
 * Since F3 `App` is the layout route — header, `<Outlet />`, `SystemStatus` — and the three
 * sections these tests pair up live in the `/` route's element. Mounting the real route table on a
 * memory router at `/` keeps the assertions about what a visitor to `/` sees, which is what they
 * were always about; rendering `<App />` alone would now find an empty outlet. `renderWithRouter`
 * (F5b) generalises this for other paths.
 */
function renderApp(): void {
  renderWithQuery(
    <RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/'] })} />,
  );
}

/**
 * T44 — structure and markup coverage for `App.tsx`'s landmark and heading wiring (task-list T44,
 * "the landmark and heading wiring in `App.tsx`"). Not behavioural: every query underneath is left
 * permanently pending (a `fetch` stub that never resolves, as `TailorPanel.test.tsx`'s loading-state
 * test already does), because the four `<section>`s and their headings render unconditionally —
 * before any list has settled — so nothing here needs a network response to exist. What is under
 * test is section/heading pairing and document order, which the 26 behavioural `TailorPanel` tests
 * do not touch at all: they render `<TailorPanel />` in isolation, never inside `<App />`.
 */

describe('App', () => {
  beforeEach(() => {
    // Every request hangs forever, so each panel sits in its own loading state and the shell's own
    // static markup — the four sections and their headings — is all that is asserted here.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('gives the tailoring section a landmark labelled by its own heading', () => {
    renderApp();

    const heading = screen.getByRole('heading', {
      level: 2,
      name: 'Your tailored CV and cover letter',
    });
    // The accessible name of a landmark comes from `aria-labelledby`, so asserting the region's
    // name equals the heading's text is the proof the two are wired together, not merely adjacent
    // in the markup.
    const region = screen.getByRole('region', { name: 'Your tailored CV and cover letter' });
    expect(region).toContainElement(heading);
  });

  it('places the tailoring section after both the base-CV and job-posting sections', () => {
    renderApp();

    const cvHeading = screen.getByRole('heading', { name: 'Your base CV' });
    const postingHeading = screen.getByRole('heading', { name: 'The job you are applying for' });
    const tailoringHeading = screen.getByRole('heading', {
      name: 'Your tailored CV and cover letter',
    });

    // `compareDocumentPosition` is the DOM-native way to ask "does A come before B" — comparing
    // `getBoundingClientRect` or array indices would be indirect proxies for the same question.
    const DOCUMENT_POSITION_FOLLOWING = 4; // Node.DOCUMENT_POSITION_FOLLOWING is undefined in jsdom
    expect(
      cvHeading.compareDocumentPosition(tailoringHeading) & DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      postingHeading.compareDocumentPosition(tailoringHeading) & DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('renders the tailoring landmark as a section distinct from the base-CV and job-posting landmarks', () => {
    renderApp();

    // Three named regions, not one merged landmark a screen-reader user would have to page through
    // as a single block.
    expect(screen.getByRole('region', { name: 'Your base CV' })).toBeInTheDocument();
    expect(
      screen.getByRole('region', { name: 'The job you are applying for' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('region', { name: 'Your tailored CV and cover letter' }),
    ).toBeInTheDocument();
  });
});
