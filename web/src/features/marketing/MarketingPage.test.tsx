import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { renderWithRouter } from '@/test/render';

/**
 * T34 RED — web-design-refresh AC-28 – AC-30: `/welcome` through the **real route table**, written
 * from the spec (C-10 and its four corrections), against a skeleton that renders an empty `<main>`.
 *
 * Query contract for the page (GREEN must satisfy it):
 * - `#how-it-works` — the steps section; its steps are the `listitem`s of one list (exactly 3).
 * - `#pricing` — the pricing section; each card is an `li` or `article` whose heading
 *   (level 2–4) is named *Guest*, *Account* or *Pro*.
 * - CTAs are real links; a label may appear in several places (header and body), so every
 *   occurrence must point at the same place.
 */

const fetchSpy = vi.fn<typeof fetch>();

beforeEach(() => {
  __resetForTests();
  fetchSpy.mockReset();
  fetchSpy.mockResolvedValue(new Response(null, { status: 401 }));
  vi.stubGlobal('fetch', fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function renderWelcome(): Promise<HTMLElement> {
  const { container } = renderWithRouter('/welcome');
  await screen.findByRole('heading', { level: 1 });
  return container;
}

function snapshot(): string[] {
  const dump = (store: Storage): string =>
    JSON.stringify(
      Array.from({ length: store.length }, (_, i) => store.key(i) ?? '')
        .sort()
        .map((k) => [k, store.getItem(k)]),
    );
  return [document.cookie, dump(localStorage), dump(sessionStorage)];
}

function section(container: HTMLElement, id: string): HTMLElement {
  const found = container.querySelector<HTMLElement>(`#${id}`);
  if (found === null) {
    throw new Error(`no #${id} section`);
  }
  return found;
}

function card(pricing: HTMLElement, name: string): HTMLElement {
  const heading = within(pricing).getByRole('heading', { name, level: 3 });
  const found = heading.closest<HTMLElement>('li, article');
  if (found === null) {
    throw new Error(`the ${name} heading is not inside an li or article`);
  }
  return found;
}

function expectEveryLink(name: string, href: string): void {
  const links = screen.getAllByRole('link', { name });
  for (const link of links) {
    expect(link).toHaveAttribute('href', href);
  }
}

describe('/welcome content — AC-28', () => {
  it('has one h1, "Every job deserves its own CV.", inside a main', async () => {
    await renderWelcome();

    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(
      screen.getByRole('heading', { level: 1, name: 'Every job deserves its own CV.' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('shows three numbered steps, with step 01 corrected to "PDF, DOCX or TXT"', async () => {
    const container = await renderWelcome();

    const steps = within(section(container, 'how-it-works')).getAllByRole('listitem');

    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent('PDF, DOCX or TXT');
  });

  it('drops the two sentences C-10 corrects out of the steps', async () => {
    await renderWelcome();

    expect(screen.queryByText(/keep it ready for every application/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/We pull out what the role is asking for/i)).not.toBeInTheDocument();
  });

  it('shows three pricing cards: Guest $0, Account $0, Pro $9 / month', async () => {
    const container = await renderWelcome();
    const pricing = section(container, 'pricing');

    expect(card(pricing, 'Guest')).toHaveTextContent('$0');
    expect(card(pricing, 'Account')).toHaveTextContent('$0');
    expect(card(pricing, 'Pro')).toHaveTextContent(/\$9\s*\/\s*month/);
  });

  it('says the Guest card deletes work after 24 hours, never "after a while"', async () => {
    const container = await renderWelcome();

    expect(card(section(container, 'pricing'), 'Guest')).toHaveTextContent(
      'Work is deleted after 24 hours',
    );
    expect(screen.queryByText(/after a while/i)).not.toBeInTheDocument();
  });
});

describe('/welcome calls to action — AC-29', () => {
  it('sends Upload my CV, Tailor my CV and Try without signing up to the workspace', async () => {
    await renderWelcome();

    expectEveryLink('Upload my CV', '/');
    expectEveryLink('Tailor my CV', '/');
    expectEveryLink('Try without signing up', '/');
  });

  it('sends Create free account to /register and Log in to /login', async () => {
    await renderWelcome();

    expectEveryLink('Create free account', '/register');
    expectEveryLink('Log in', '/login');
  });

  it('makes Go Pro a disabled button described by "Coming soon — nothing is charged"', async () => {
    await renderWelcome();

    const goPro = screen.getByRole('button', { name: 'Go Pro' });
    const noteId = goPro.getAttribute('aria-describedby');

    expect(goPro).toBeDisabled();
    expect(noteId).not.toBeNull();
    expect(document.getElementById(noteId ?? '')).toHaveTextContent(
      'Coming soon — nothing is charged',
    );
  });
});

describe('/welcome sends and stores nothing — AC-30', () => {
  it('calls fetch zero times', async () => {
    await renderWelcome();

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('leaves document.cookie, localStorage and sessionStorage unchanged', async () => {
    const before = snapshot();

    await renderWelcome();

    expect(snapshot()).toEqual(before);
  });

  it('does not mount the shell: no System status anywhere', async () => {
    await renderWelcome();

    expect(screen.queryByRole('heading', { name: /system status/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/system status/i)).not.toBeInTheDocument();
    for (const banner of screen.queryAllByRole('banner')) {
      expect(banner).not.toHaveTextContent(/system status/i);
    }
  });
});
