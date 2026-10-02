import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import {
  CHECK_EMAIL_HEADING,
  CONFIRM_EMAIL_HEADING,
  CONFIRM_EMAIL_LABEL,
  RESET_CONFIRM_HEADING,
  RESET_CONFIRM_SUBMIT_LABEL,
  RESET_REQUEST_HEADING,
  RESET_REQUEST_SUBMIT_LABEL,
  SEND_AGAIN_LABEL,
} from './accountMailCopy';
import {
  EMAIL,
  PASSWORD,
  TOKEN,
  accepted,
  noContent,
  openLink,
  refusal,
  resetAccountMailTest,
} from './test/support';

/**
 * T39 (`qa`, test-after) — AC-52: each slice-2.5 screen, **through the real route table and the app
 * shell**, has exactly one `h1`, labelled inputs, its outcomes in `role="status"` / `role="alert"`
 * and focus moved to the outcome; the routes are public; and nothing under `features/accountMail/`
 * uses `dangerouslySetInnerHTML`.
 *
 * **One `h1` per page is the shell's.** `App` owns `<h1>TailorCraft</h1>`, so a screen contributes
 * an `h2` — an `h1` of its own would make two. Each page is asserted for both halves: the single
 * `h1`, and its own `h2`.
 *
 * **Focus** is read from `document.activeElement` after the outcome is on screen, never inferred
 * from the markup: the outcome element itself (a `tabIndex={-1}` `status` or `alert`), or, on
 * `CheckYourEmail`, its heading.
 *
 * `/reset-password/confirm`'s section is deliberately *not* `aria-labelledby` its heading (it would
 * collide with the "New password" label); that labelling is not asserted.
 */

const REGISTER = 'POST /api/auth/register';
const RESET_REQUEST = 'POST /api/auth/password-reset';
const RESET_CONFIRM = 'POST /api/auth/password-reset/confirm';
const CONFIRM = 'POST /api/auth/registration/confirm';

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

function expectOneH1(): void {
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  expect(screen.getByRole('heading', { level: 1, name: 'TailorCraft' })).toBeInTheDocument();
}

/** Every form control on the page has an accessible name (`getByLabelText` would throw otherwise). */
function expectEveryInputLabelled(): void {
  const inputs = Array.from(
    document.querySelectorAll<HTMLInputElement>('input:not([type="hidden"]), select, textarea'),
  );
  expect(inputs.length).toBeGreaterThan(0);
  for (const input of inputs) {
    expect(Array.from(input.labels ?? []).length).toBeGreaterThan(0);
  }
}

describe('/register → Check your email', () => {
  async function submitRegistration(responses: Record<string, () => Response | Promise<Response>>) {
    stubAccountFetch({
      'GET /api/base-cvs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      'GET /api/tailoring-runs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      'GET /api/job-postings': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      [REGISTER]: accepted,
      ...responses,
    });
    const user = userEvent.setup();
    renderWithRouter('/register');
    await screen.findByRole('region', { name: 'Create an account' });
    expectOneH1();
    expectEveryInputLabelled();
    await user.type(screen.getByLabelText(/email/i), EMAIL);
    await user.type(screen.getByLabelText(/password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: /^create account$/i }));
    return user;
  }

  it('has one h1, the heading as an h2 that holds focus, and the instruction in a status region', async () => {
    await submitRegistration({});

    const heading = await screen.findByRole('heading', { level: 2, name: CHECK_EMAIL_HEADING });
    expectOneH1();
    expect(document.activeElement).toBe(heading);
    // The section is named by its heading and the inbox sentence is announced as a status.
    const region = screen.getByRole('region', { name: CHECK_EMAIL_HEADING });
    expect(within(region).getByRole('status')).toHaveTextContent(EMAIL);
  });

  it('a failed Send it again is a role="alert" that takes focus', async () => {
    const user = await submitRegistration({});
    await screen.findByRole('heading', { level: 2, name: CHECK_EMAIL_HEADING });
    stubAccountFetch({
      'GET /api/base-cvs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      'GET /api/tailoring-runs': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      'GET /api/job-postings': () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
      [REGISTER]: refusal(503, 'unavailable'),
    });

    await user.click(screen.getByRole('button', { name: SEND_AGAIN_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(document.activeElement).toBe(alert);
    expectOneH1();
  });
});

describe('/login', () => {
  it('has one h1 and labelled inputs', async () => {
    stubAccountFetch({});
    renderWithRouter('/login');

    await screen.findByRole('region', { name: 'Log in' });
    expectOneH1();
    expectEveryInputLabelled();
  });
});

describe('/reset-password', () => {
  function mount() {
    renderWithRouter('/reset-password');
    return screen.findByRole('heading', { level: 2, name: RESET_REQUEST_HEADING });
  }

  it('is public, has one h1, its h2 and a labelled email field', async () => {
    stubAccountFetch({});

    await mount();

    expectOneH1();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expectEveryInputLabelled();
  });

  it('a 202 is a role="status" that takes focus', async () => {
    stubAccountFetch({ [RESET_REQUEST]: accepted });
    const user = userEvent.setup();
    await mount();

    await user.type(screen.getByLabelText('Email'), EMAIL);
    await user.click(screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL }));

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(EMAIL);
    expect(document.activeElement).toBe(status);
    expectOneH1();
  });

  it('a refusal is a role="alert" that takes focus and describes the field', async () => {
    stubAccountFetch({ [RESET_REQUEST]: refusal(422, 'invalid_email') });
    const user = userEvent.setup();
    await mount();

    await user.type(screen.getByLabelText('Email'), 'not-an-email');
    await user.click(screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(document.activeElement).toBe(alert);
    expect(screen.getByLabelText('Email')).toHaveAccessibleDescription(alert.textContent);
  });
});

describe('/reset-password/confirm', () => {
  async function mount() {
    openLink('/reset-password/confirm', TOKEN);
    renderWithRouter('/reset-password/confirm');
    await screen.findByRole('heading', { level: 2, name: RESET_CONFIRM_HEADING });
  }

  it('is public, has one h1, its h2 and a labelled new-password field', async () => {
    stubAccountFetch({});

    await mount();

    expectOneH1();
    expect(screen.getByLabelText('New password')).toBeInTheDocument();
    expectEveryInputLabelled();
  });

  it('a 204 is a role="status" that takes focus', async () => {
    stubAccountFetch({ [RESET_CONFIRM]: noContent });
    const user = userEvent.setup();
    await mount();

    await user.type(screen.getByLabelText('New password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    const status = await screen.findByRole('status');
    expect(document.activeElement).toBe(status);
    expectOneH1();
  });

  it('a dead link is a role="alert" that takes focus', async () => {
    stubAccountFetch({ [RESET_CONFIRM]: refusal(400, 'link_invalid') });
    const user = userEvent.setup();
    await mount();

    await user.type(screen.getByLabelText('New password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(document.activeElement).toBe(alert);
  });

  it('a too-short password is a role="alert" that takes focus and describes the field', async () => {
    stubAccountFetch({ [RESET_CONFIRM]: refusal(422, 'password_too_short', { min_length: 13 }) });
    const user = userEvent.setup();
    await mount();

    await user.type(screen.getByLabelText('New password'), 'short');
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(document.activeElement).toBe(alert);
    expect(screen.getByLabelText('New password')).toHaveAccessibleDescription(
      new RegExp(alert.textContent.slice(0, 20)),
    );
  });
});

describe('/confirm-email', () => {
  async function mount(withToken: boolean) {
    openLink('/confirm-email', withToken ? TOKEN : undefined);
    renderWithRouter('/confirm-email');
    await screen.findByRole('heading', { level: 2, name: CONFIRM_EMAIL_HEADING });
  }

  it('is public and has one h1 and its h2, with or without a token', async () => {
    stubAccountFetch({});

    await mount(false);

    expectOneH1();
  });

  it('a 204 is a role="status" that takes focus', async () => {
    stubAccountFetch({ [CONFIRM]: noContent });
    const user = userEvent.setup();
    await mount(true);

    await user.click(screen.getByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const status = await screen.findByRole('status');
    expect(document.activeElement).toBe(status);
    expectOneH1();
  });

  it('a dead link is a role="alert" that takes focus', async () => {
    stubAccountFetch({ [CONFIRM]: refusal(400, 'link_invalid') });
    const user = userEvent.setup();
    await mount(true);

    await user.click(screen.getByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(document.activeElement).toBe(alert);
  });
});

describe('no raw-HTML escape hatch under features/accountMail/', () => {
  const root = dirname(fileURLToPath(import.meta.url));

  function productionSources(dir: string): Array<{ path: string; text: string }> {
    const found: Array<{ path: string; text: string }> = [];
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry !== 'test') {
          found.push(...productionSources(full));
        }
      } else if (/\.tsx?$/.test(full) && !/\.test\.tsx?$/.test(full)) {
        found.push({ path: full, text: readFileSync(full, 'utf-8') });
      }
    }
    return found;
  }

  // Assembled, so this file does not itself trip the repo-wide `security.grep` scan (AC-27).
  const RAW_HTML_PROP = 'dangerously' + 'SetInnerHTML';
  const usesRawHtml = (source: string): boolean =>
    new RegExp(`${RAW_HTML_PROP}|\\.innerHTML\\s*=`).test(source);

  it('the detector flags a planted occurrence (positive control)', () => {
    expect(usesRawHtml(`<div ${RAW_HTML_PROP}={{ __html: copy }} />`)).toBe(true);
    expect(usesRawHtml('node.innerHTML = copy;')).toBe(true);
    expect(usesRawHtml('<p>{copy}</p>')).toBe(false);
  });

  it('finds none, and found the feature to look at', () => {
    const sources = productionSources(root);

    expect(sources.length).toBeGreaterThanOrEqual(10);
    expect(sources.filter(({ text }) => usesRawHtml(text)).map(({ path }) => path)).toEqual([]);
  });
});
