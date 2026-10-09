import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { __resetForTests } from '@/features/auth/authStore';
import { LoginPage } from '@/features/auth/components/LoginPage';
import { RegisterPage } from '@/features/auth/components/RegisterPage';
import { deleteAccountErrorCopy } from '@/features/savedCvs/savedCvsCopy';
import {
  RESET_REQUEST_SUBMIT_LABEL,
  SEND_AGAIN_LABEL,
} from '@/features/accountMail/accountMailCopy';
import { CheckYourEmail } from '@/features/accountMail/components/CheckYourEmail';
import { PasswordResetRequestPage } from '@/features/accountMail/components/PasswordResetRequestPage';
import {
  EMAIL,
  PASSWORD,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '@/features/accountMail/test/support';
import { KEEP_LABEL, OFFER_REGION_LABEL } from '@/features/claim/claimCopy';
import {
  CLAIM_PATH,
  guestCv,
  guestListRoutes,
  guestRun,
  renderOffer,
} from '@/features/claim/test/support';
import { SaveIndicator } from '@/features/editor/components/SaveIndicator';
import {
  moveFailureCopy,
  retitleFailureCopy,
  trackFailureCopy,
} from '@/features/tracking/trackingCopy';
import { USER_A, callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';

import type { RouteHandler } from '@/test/accountFetch';
import type { ReactElement } from 'react';

/**
 * T18 RED (slice 3.3; AC-14, AC-15, AC-16) — the account-side surfaces learn the hold.
 *
 * Same time base as T14/T16: zero-time flushes after a click, deadlines computed from the clock as
 * it reads (`advanceToward`), never a fixed `N - 1`. A hold is `toBeDisabled()` **and** no request;
 * a release is `toBeEnabled()`; each absence has a positive control (the control worked before, and
 * a 503 on the same control is a re-clickable button).
 *
 * The existing `GuestWorkOffer.test.tsx` runs unedited (the refactor's proof, AC-14); the cases here
 * are additions. Two older tests state the superseded fixed copy and are corrected beside this file,
 * naming AC-15 (`LoginPage.test.tsx` I-14/I-15) and AC-15/V-61 (`CheckYourEmail.test.tsx`).
 */

// Verify's MINOR 1: once a hold is released, no sentence may still name the wait.
const WAIT_NAMED = /try again (?:at \d|in \d+ seconds?)/i;

const NOW = 1_700_000_000_000; // 22:13:20 UTC
const WINDOW_MS = 120_000;
// The browser's locale decides the shape (jsdom: en-US, "10:16 PM"); AC-5 forbids forcing one.
const CLOCK_TIME = /\d{1,2}:\d{2}(?:\s?[AP]M)?/;
const SERVER_PROSE = 'server prose';

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Zero-time by default; the account boot needs fake-time steps to load (T14's finding). */
async function settle(stepMs = 0): Promise<void> {
  for (let i = 0; i < 8; i += 1) {
    await advance(stepMs);
  }
}

async function advanceToward(deadlineMs: number, offsetMs: number): Promise<void> {
  await advance(deadlineMs + offsetMs - Date.now());
}

const tooMany = (retryAfter = '120') =>
  refusal(429, 'rate_limited', {}, { 'Retry-After': retryAfter });

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  resetAccountMailTest();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
  __resetForTests();
});

// --- AC-14: the claim offer ------------------------------------------------------------------------

describe('AC-14 — Keep them in my account', () => {
  function claimRoutes(claim: RouteHandler) {
    return stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      [`POST ${CLAIM_PATH}`]: claim,
    });
  }
  const keep = () =>
    within(screen.getByRole('region', { name: OFFER_REGION_LABEL })).getByRole('button', {
      name: KEEP_LABEL,
    });

  beforeEach(() => {
    signInAs(USER_A);
  });

  it('a 429 holds the button, names the time, sends nothing on click, and releases at the deadline', async () => {
    const fetch = claimRoutes(tooMany());
    renderOffer();
    await settle(10);
    expect(keep()).toBeEnabled(); // positive control

    const clickedAt = Date.now();
    fireEvent.click(keep());
    await settle();

    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
    expect(keep()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(CLOCK_TIME);
    fireEvent.click(keep());
    await settle();
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(keep()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(keep()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);

    await advance(5000);
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
  });

  it('a 429 within 90 s names the wait in seconds', async () => {
    claimRoutes(tooMany('45'));
    renderOffer();
    await settle(10);

    fireEvent.click(keep());
    await settle();

    expect(screen.getByRole('alert')).toHaveTextContent(/in 45 seconds/);
  });

  it('a 503 holds nothing', async () => {
    const fetch = claimRoutes(refusal(503, 'service_unavailable'));
    renderOffer();
    await settle(10);

    fireEvent.click(keep());
    await settle();

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(keep()).toBeEnabled();
    fireEvent.click(keep());
    await settle();
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(2);
  });
});

// --- AC-15: credentials forms and mail -------------------------------------------------------------

interface Surface {
  readonly name: string;
  readonly path: string;
  readonly mount: () => void;
  readonly fill: () => void;
  readonly submit: () => HTMLElement;
}

const fillCredentials = (): void => {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: EMAIL } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: PASSWORD } });
};

function mountPage(element: ReactElement, routePath: string): void {
  renderPage(element, { routePath });
}

const SURFACES: readonly Surface[] = [
  {
    name: 'log in',
    path: 'POST /api/auth/login',
    mount: () => {
      mountPage(<LoginPage />, '/login');
    },
    fill: fillCredentials,
    submit: () => screen.getByRole('button', { name: /^log in$/i }),
  },
  {
    name: 'register',
    path: 'POST /api/auth/register',
    mount: () => {
      mountPage(<RegisterPage />, '/register');
    },
    fill: fillCredentials,
    submit: () => screen.getByRole('button', { name: /create account/i }),
  },
  {
    name: 'reset request',
    path: 'POST /api/auth/password-reset',
    mount: () => {
      mountPage(<PasswordResetRequestPage />, '/reset-password');
    },
    fill: () => {
      fireEvent.change(screen.getByLabelText(/email/i), { target: { value: EMAIL } });
    },
    submit: () => screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL }),
  },
  {
    name: 'Send it again',
    path: 'POST /api/auth/register',
    mount: () => {
      mountPage(
        <CheckYourEmail
          credentials={{ email: EMAIL, password: PASSWORD }}
          next={null}
          hasGuestWork={false}
          onUseDifferentEmail={() => undefined}
        />,
        '/register',
      );
    },
    fill: () => undefined,
    submit: () => screen.getByRole('button', { name: SEND_AGAIN_LABEL }),
  },
];

describe.each(SURFACES)('AC-15 — $name', (surface) => {
  const posts = (fetch: ReturnType<typeof stubAccountFetch>) =>
    callsTo(fetch, 'POST', surface.path.replace('POST ', '')).length;

  it('a 429 holds the submit, names the time, sends nothing on click, and releases', async () => {
    const fetch = stubAccountFetch({ [surface.path]: tooMany() });
    surface.mount();
    await settle();
    surface.fill();
    expect(surface.submit()).toBeEnabled(); // positive control

    const clickedAt = Date.now();
    fireEvent.click(surface.submit());
    await settle();

    expect(posts(fetch)).toBe(1);
    expect(surface.submit()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(CLOCK_TIME);
    expect(document.body).not.toHaveTextContent(SERVER_PROSE);
    fireEvent.click(surface.submit());
    await settle();
    expect(posts(fetch)).toBe(1);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(surface.submit()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(surface.submit()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);

    await advance(5000);
    expect(posts(fetch)).toBe(1);
  });

  it('a 503 holds nothing: the same submit can be pressed again', async () => {
    const fetch = stubAccountFetch({ [surface.path]: refusal(503, 'service_unavailable') });
    surface.mount();
    await settle();
    surface.fill();

    fireEvent.click(surface.submit());
    await settle();

    expect(screen.getByRole('alert')).toBeInTheDocument(); // the refusal landed
    expect(surface.submit()).toBeEnabled();
    fireEvent.click(surface.submit());
    await settle();
    expect(posts(fetch)).toBe(2);
  });
});

// --- AC-16: copy only ------------------------------------------------------------------------------

describe('AC-16 — copy through retryPhrase, nothing held', () => {
  const rateLimited = (seconds: number) =>
    new ApiError(429, SERVER_PROSE, 'rate_limited', {}, seconds);

  describe.each([
    ['a move', moveFailureCopy],
    ['a retitle', retitleFailureCopy],
    ['Add to board', trackFailureCopy],
    ['account deletion', deleteAccountErrorCopy],
  ] as const)('%s', (_name, copyFor) => {
    it('names a wait beyond 90 s as a clock time', () => {
      expect(copyFor(rateLimited(120), NOW + 120_000, NOW)).toMatch(CLOCK_TIME);
    });

    it('names a short wait in seconds, from the deadline and not the raw header', () => {
      expect(copyFor(rateLimited(45), NOW + 45_000, NOW)).toMatch(/in 45 seconds/);
    });

    it('never says "a moment" or "a few minutes" for a wait it knows', () => {
      expect(copyFor(rateLimited(120), NOW + 120_000, NOW)).not.toMatch(/a moment|a few minutes/i);
    });

    it('leaves a 503 as it was (no clock time invented)', () => {
      const error = new ApiError(503, SERVER_PROSE, 'service_unavailable');
      expect(copyFor(error, null, NOW)).not.toMatch(CLOCK_TIME);
    });
  });
});

describe('AC-16 — autosave paused (SaveIndicator)', () => {
  it('names the clock time the save resumes', () => {
    render(
      <SaveIndicator state={{ kind: 'paused', retryAfterSeconds: 120, untilMs: NOW + 120_000 }} />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(
      /^Saving paused — we'll save again automatically at \d{1,2}:\d{2}(?:\s?[AP]M)?\.$/,
    );
  });

  it('names a short wait in seconds', () => {
    render(
      <SaveIndicator state={{ kind: 'paused', retryAfterSeconds: 45, untilMs: NOW + 45_000 }} />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(
      "Saving paused — we'll save again automatically in 45 seconds.",
    );
  });

  it('is fixed text: a re-render later does not reword the wait inside the live region', () => {
    const state = { kind: 'paused', retryAfterSeconds: 45, untilMs: NOW + 45_000 } as const;
    const { rerender } = render(<SaveIndicator state={state} />);
    vi.setSystemTime(NOW + 20_000);

    rerender(<SaveIndicator state={{ ...state }} />);

    expect(screen.getByRole('status')).toHaveTextContent(
      "Saving paused — we'll save again automatically in 45 seconds.",
    );
  });

  it('no longer prints the raw header value', () => {
    render(
      <SaveIndicator state={{ kind: 'paused', retryAfterSeconds: 120, untilMs: NOW + 120_000 }} />,
    );

    expect(screen.getByRole('status')).not.toHaveTextContent(/120 s|limit resets/);
  });
});

// --- AC-16: no raw seconds anywhere in shipped copy -----------------------------------------------

describe('AC-16 — no raw Retry-After seconds reach the copy', () => {
  const sources = import.meta.glob('/src/**/*.{ts,tsx}', {
    query: '?raw',
    import: 'default',
    eager: true,
  });
  // `hold.ts` is the one place allowed to turn seconds into words: `retryPhrase` is the rule.
  const shipped = Object.entries(sources).filter(
    ([path]) => !/\.test\.|\/test\/|\/features\/retry\/hold\.ts$/.test(path),
  ) as ReadonlyArray<[string, string]>;

  it('has a positive control: the glob sees the source tree', () => {
    expect(shipped.length).toBeGreaterThan(100);
  });

  it('no copy interpolates a raw seconds value, and none divides retryAfterSeconds into minutes', () => {
    const offenders = shipped
      .filter(
        ([, text]) =>
          /\$\{String\([\w.]*(?:retryAfterSeconds|seconds)\)\}/.test(text) ||
          /retryAfterSeconds\s*\/\s*60/.test(text),
      )
      .map(([path]) => path);

    expect(offenders).toEqual([]);
  });
});
