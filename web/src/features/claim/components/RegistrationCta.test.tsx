import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import {
  CTA_LOGIN_LABEL,
  CTA_REGION_LABEL,
  CTA_REGISTER_LABEL,
  CTA_RETENTION_LINE,
  CTA_SENTENCE,
  NOT_NOW_LABEL,
} from '../claimCopy';
import { RegistrationCta } from './RegistrationCta';

/**
 * T30 RED — the registration CTA, presentational (AC-35). Against T29's skeleton the component
 * renders `null`: every test is red on "unable to find role region" — and each absence assertion
 * below is paired with the region having been found first, so a skeleton cannot satisfy it.
 */

function renderCta(next = '/runs/run-1/cv') {
  return render(
    <MemoryRouter>
      <RegistrationCta next={next} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('RegistrationCta (AC-35)', () => {
  it('is one region labelled "Save your work" with the PRD sentence and the retention line', () => {
    renderCta();

    const region = screen.getByRole('region', { name: CTA_REGION_LABEL });
    expect(within(region).getByText(CTA_SENTENCE)).toBeInTheDocument();
    expect(within(region).getByText(CTA_RETENTION_LINE)).toBeInTheDocument();
    expect(CTA_RETENTION_LINE).toBe('Your work here is deleted within 24 hours.');
  });

  it('links Create an account to /register with this page as `next`', () => {
    renderCta('/runs/run-1/cv');

    const region = screen.getByRole('region', { name: CTA_REGION_LABEL });
    expect(within(region).getByRole('link', { name: CTA_REGISTER_LABEL })).toHaveAttribute(
      'href',
      '/register?next=%2Fruns%2Frun-1%2Fcv',
    );
  });

  it('links Sign in to /login with this page as `next`', () => {
    renderCta('/runs/run-1/cover_letter');

    const region = screen.getByRole('region', { name: CTA_REGION_LABEL });
    expect(within(region).getByRole('link', { name: CTA_LOGIN_LABEL })).toHaveAttribute(
      'href',
      '/login?next=%2Fruns%2Frun-1%2Fcover_letter',
    );
  });

  it('percent-encodes a next that carries a query, so it survives as one parameter', () => {
    renderCta('/runs/a b/cv');

    const region = screen.getByRole('region', { name: CTA_REGION_LABEL });
    const href = within(region)
      .getByRole('link', { name: CTA_REGISTER_LABEL })
      .getAttribute('href');
    expect(new URL(href ?? '', 'http://x').searchParams.get('next')).toBe('/runs/a b/cv');
  });

  it('Not now hides the region, for this mount only, and touches no browser storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const user = userEvent.setup();
    const { unmount } = renderCta();
    const region = screen.getByRole('region', { name: CTA_REGION_LABEL });

    await user.click(within(region).getByRole('button', { name: NOT_NOW_LABEL }));

    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    expect(setItem).not.toHaveBeenCalled();
    unmount();
    renderCta();
    expect(screen.getByRole('region', { name: CTA_REGION_LABEL })).toBeInTheDocument();
  });
});
