import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { BaseCvCard } from '@/features/intake/components/BaseCvCard';

import { SavedBaseCvPicker } from './components/SavedBaseCvPicker';

import type { SavedBaseCvPickerProps } from './components/SavedBaseCvPicker';
import type { BaseCv } from '@/features/intake/types';

/**
 * T32 RED — AC-44 (feature-spec.md): the copy is gone from the client. Registered users now
 * *claim* a guest workspace; nothing in the browser may create a working copy any more
 * (`POST /api/base-cvs/copies` answers 405 from T19 on).
 *
 * Three independent proofs, none naming an internal symbol where a user-visible one exists:
 * (1) the picker, in every mode a caller could still ask for, offers no *Use this CV* action and
 * posts nothing when every button on it is pressed; (2) a grep over production source finds no
 * request to the removed route and no `copySavedBaseCv` symbol, with a positive control proving the
 * grep can find a string that IS there; (3) the *Working copy* badge still renders — copies that
 * already exist keep their label.
 */

const USER = { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-25T10:00:00Z' };
const AUTHENTICATED = {
  access_token: 'token-1',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: USER,
};

const SAVED_CV = {
  id: 'saved-cv-1',
  label: null,
  original_filename: 'jane-resume.pdf',
  content_type: 'application/pdf',
  size_bytes: 2048,
  status: 'extracted',
  character_count: 512,
  failure_reason: null,
  failure_message: null,
  uploaded_at: '2026-09-20T10:00:00Z',
};

function stubFetch() {
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    if (method === 'GET' && url === '/api/auth/me') return Promise.resolve(jsonResponse(200, USER));
    if (method === 'GET' && url === '/api/me/base-cvs') {
      return Promise.resolve(jsonResponse(200, { items: [SAVED_CV] }));
    }
    if (url === '/api/base-cvs/copies') {
      return Promise.resolve(
        jsonResponse(201, { id: 'guest-cv-new', origin: 'copied_from_saved' }),
      );
    }
    return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function copyPosts(mock: ReturnType<typeof stubFetch>): string[] {
  return mock.mock.calls
    .map(
      ([input, init]) =>
        `${init?.method ?? 'GET'} ${typeof input === 'string' ? input : input.toString()}`,
    )
    .filter((call) => call.includes('/api/base-cvs/copies'));
}

beforeEach(() => {
  __resetForTests();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// The union no longer admits 'copy' after T33, and a test must not fail to *compile* to prove it:
// the value is built through a cast so the same file runs before and after.
const MODES: readonly (readonly [string, SavedBaseCvPickerProps])[] = [
  // Redundant while the union still admits no props, required once T33 makes `mode` mandatory; the
  // rule can only see one side of that, so it is silenced for this line only.
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
  ['no props (the former default)', {} as unknown as SavedBaseCvPickerProps],
  [
    "mode: 'copy' (the former explicit mode)",
    { mode: 'copy' } as unknown as SavedBaseCvPickerProps,
  ],
];

describe('AC-44: the picker has no copy action and posts nothing', () => {
  it.each(MODES)('%s: no "Use this CV" button is offered', async (_name, props) => {
    stubFetch();
    authStore.setAuthenticated(AUTHENTICATED);
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
        }
      >
        <MemoryRouter>
          <SavedBaseCvPicker {...props} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findAllByRole('radio').catch(() => []);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(screen.queryByRole('button', { name: /use this cv/i })).not.toBeInTheDocument();
  });

  it.each(MODES)(
    '%s: pressing every control sends no request to /api/base-cvs/copies',
    async (_name, props) => {
      const mock = stubFetch();
      authStore.setAuthenticated(AUTHENTICATED);
      render(
        <QueryClientProvider
          client={
            new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
          }
        >
          <MemoryRouter>
            <SavedBaseCvPicker {...props} />
          </MemoryRouter>
        </QueryClientProvider>,
      );
      await new Promise((resolve) => setTimeout(resolve, 20));
      for (const radio of screen.queryAllByRole('radio')) fireEvent.click(radio);
      for (const button of screen.queryAllByRole('button')) fireEvent.click(button);
      await new Promise((resolve) => setTimeout(resolve, 20));

      expect(copyPosts(mock)).toEqual([]);
    },
  );
});

// --- the grep -----------------------------------------------------------------------------------

const THIS_FILE = fileURLToPath(import.meta.url);
const SRC_ROOT = join(dirname(THIS_FILE), '..', '..');

interface SourceLine {
  readonly path: string;
  readonly text: string;
}

function productionLines(dir: string): SourceLine[] {
  const out: SourceLine[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry !== 'node_modules' && entry !== 'dist') out.push(...productionLines(full));
      continue;
    }
    if (!['.ts', '.tsx'].includes(extname(full)) || /\.test\.tsx?$/.test(full)) continue;
    for (const text of readFileSync(full, 'utf-8').split('\n')) out.push({ path: full, text });
  }
  return out;
}

function isComment(text: string): boolean {
  const t = text.trim();
  return t.startsWith('//') || t.startsWith('/*') || t.startsWith('*');
}

function find(needle: string, lines: readonly SourceLine[]): string[] {
  return lines
    .filter((l) => l.text.includes(needle) && !isComment(l.text))
    .map((l) => `${l.path}: ${l.text.trim()}`);
}

describe('AC-44: no request to the removed copy route is reachable from production code', () => {
  it('positive control: the same scan finds a string that is known to be there', () => {
    const lines = productionLines(SRC_ROOT);
    expect(find('/api/me/base-cvs', lines).length).toBeGreaterThan(0);
    // ...and a planted line, comment-filtering included.
    const planted: SourceLine[] = [
      { path: 'planted.ts', text: "request('/api/base-cvs/copies')" },
      { path: 'planted.ts', text: "// '/api/base-cvs/copies' in a comment" },
    ];
    expect(find('/api/base-cvs/copies', planted)).toHaveLength(1);
  });

  it('the copies route is named nowhere in production code outside comments', () => {
    expect(find('/api/base-cvs/copies', productionLines(SRC_ROOT))).toEqual([]);
  });

  it('copySavedBaseCv is named nowhere in production code outside comments', () => {
    expect(find('copySavedBaseCv', productionLines(SRC_ROOT))).toEqual([]);
  });
});

// --- the badge stays ----------------------------------------------------------------------------

describe('AC-44: the Working copy badge survives', () => {
  it('still renders for a CV whose origin is copied_from_saved', () => {
    const cv: BaseCv = {
      id: '0192f0a1-0000-7000-8000-000000000001',
      original_filename: 'jane-cv.pdf',
      content_type: 'application/pdf',
      size_bytes: 2048,
      status: 'extracted',
      character_count: 512,
      failure_reason: null,
      failure_message: null,
      uploaded_at: '2026-09-07T10:00:00Z',
      expires_at: '2026-09-08T10:00:00Z',
      origin: 'copied_from_saved',
    };

    render(<BaseCvCard cv={cv} onReplace={vi.fn()} />);

    expect(screen.getByText('Working copy')).toBeInTheDocument();
  });
});
