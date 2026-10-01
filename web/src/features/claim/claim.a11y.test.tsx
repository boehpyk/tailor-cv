import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, ok, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import { CLAIM_FAILED, KEEP_LABEL, OFFER_REGION_LABEL } from './claimCopy';
import { RegistrationCta } from './components/RegistrationCta';
import {
  CLAIM_PATH,
  claimResult,
  guestCv,
  guestListRoutes,
  guestRun,
  renderOffer,
} from './test/support';

/**
 * T34 (`qa`, test-after) — AC-45, the accessibility half: the claim surface is announced the way
 * the spec says, and each assertion below was observed RED under a named mutation of the production
 * component (source restored byte-exact with `git checkout`; both outcomes recorded here).
 *
 * Mutations and what each one did to THIS file (all three went green again on restore):
 *
 * - `GuestWorkOffer.tsx`: `role="status"` removed from the success `<p>` → "success is announced
 *   by one role=status" red: `Unable to find role="status"`.
 * - `RegistrationCta.tsx`: `aria-label={CTA_REGION_LABEL}` removed → "the CTA is a labelled region"
 *   red: `Unable to find an accessible element with the role "region"` — an unlabelled `<section>`
 *   has no region role, which is how a lost label is observable.
 * - `GuestWorkOffer.tsx`: the `role="alert"` paragraph moved out of the `<section>` into a fragment
 *   sibling → "a failure is announced by role=alert INSIDE the offer region" red:
 *   `Unable to find role="alert"` (queried within the region).
 *
 * (The behavioural suites already find these roles by `findByRole`; this file pins the *placement*
 * and the *naming* contract on one screen, so a refactor that keeps the roles but loses the labels
 * cannot pass.)
 */

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

/** Every `<section>` must be exposed as a region: an unlabelled one silently is not. */
function expectEverySectionIsANamedRegion(container: HTMLElement): void {
  const sections = container.querySelectorAll('section');
  expect(sections.length).toBeGreaterThan(0);
  expect(screen.getAllByRole('region')).toHaveLength(sections.length);
}

function expectEveryControlHasAName(scope: HTMLElement): void {
  const controls = [
    ...within(scope).getAllByRole('button'),
    ...within(scope).queryAllByRole('link'),
  ];
  expect(controls.length).toBeGreaterThan(0);
  for (const control of controls) {
    expect(control, control.outerHTML).toHaveAccessibleName();
    expect(control.getAttribute('aria-label') ?? control.textContent).not.toBe('');
  }
}

describe('AC-45: the registration CTA', () => {
  it('is a labelled region whose buttons and links all have accessible names', () => {
    const { container } = render(
      <MemoryRouter>
        <RegistrationCta next="/runs/r/cv" />
      </MemoryRouter>,
    );

    expectEverySectionIsANamedRegion(container);
    expectEveryControlHasAName(screen.getByRole('region', { name: 'Save your work' }));
  });

  it('announces nothing by itself: no status and no alert', () => {
    render(
      <MemoryRouter>
        <RegistrationCta next="/runs/r/cv" />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('AC-45: the claim offer', () => {
  it('idle: a labelled region, named buttons, and no live region yet', async () => {
    stubAccountFetch(guestListRoutes([guestCv()], [guestRun()]));

    const { container } = renderOffer();
    const region = await screen.findByRole('region', { name: OFFER_REGION_LABEL });

    expectEverySectionIsANamedRegion(container);
    expectEveryControlHasAName(region);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a failure is announced by role=alert INSIDE the offer region, never as a status', async () => {
    stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      [`POST ${CLAIM_PATH}`]: () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
    });
    renderOffer();
    const region = await screen.findByRole('region', { name: OFFER_REGION_LABEL });

    await userEvent.setup().click(within(region).getByRole('button', { name: KEEP_LABEL }));

    const alert = await within(region).findByRole('alert');
    expect(alert).toHaveTextContent(CLAIM_FAILED);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('success is announced by one role=status, and the offer region is gone', async () => {
    stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      [`POST ${CLAIM_PATH}`]: ok(claimResult()),
    });
    renderOffer();
    const region = await screen.findByRole('region', { name: OFFER_REGION_LABEL });

    await userEvent.setup().click(within(region).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findAllByRole('status')).toHaveLength(1);
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
