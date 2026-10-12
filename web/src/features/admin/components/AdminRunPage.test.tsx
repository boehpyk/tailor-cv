import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { downloadFilenameFor } from '@/features/export/exportCopy';
import { saveBlob } from '@/features/export/saveBlob';
import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import {
  ADMIN,
  TARGET_ID,
  contentRoutes,
  entry,
  exportJob,
  succeededRun,
  world,
} from '@/test/adminContentFetch';
import { renderWithRouter } from '@/test/render';

import {
  CV_DELETED_NOTE,
  DOWNLOAD_FILE_GONE,
  RUN_GONE,
  RUN_STILL_RUNNING,
  USER_LOADING,
  historyStatusCopy,
} from '../adminCopy';

import type { ContentWorld } from '@/test/adminContentFetch';
import type { RouteHandler } from '@/test/accountFetch';

vi.mock('@/features/export/saveBlob', () => ({ saveBlob: vi.fn() }));

/**
 * T12 RED — slice 4.3, AC-28 (the run page) and AC-25 for both documents. `saveBlob` is spied (not
 * `URL.createObjectURL`). Against T11's skeleton (a fixed "Loading…" status) each fails on an
 * absent element; the loading test passes against it (a regression guard).
 */

beforeEach(() => {
  __resetForTests();
  vi.mocked(saveBlob).mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__pwned;
});

const P = '/api/admin/users/:id';
const RUN_PATH = `/admin/users/${TARGET_ID}/runs/run-1`;
const SLOW = { timeout: 4000 };

function open(w: ContentWorld = world(), overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch(contentRoutes(w, overrides));
  signInAs(ADMIN);
  const view = renderWithRouter(RUN_PATH, { queryClient: createAppQueryClient() });
  return { fetch, ...view };
}

async function section(heading: string): Promise<HTMLElement> {
  const h = await screen.findByRole('heading', { level: 2, name: heading });
  const found = h.closest('section');
  if (found === null) throw new Error(`no <section> around "${heading}"`);
  return found;
}

async function clickWhenEnabled(button: HTMLElement): Promise<void> {
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

describe('the run page — AC-28', () => {
  it('loading: a status line while the run is pending', async () => {
    const { fetch } = open(world(), { [`GET ${P}/tailoring-runs/:rid`]: hang });

    // Not the lazy chunk's Suspense fallback (also a status): wait until the page asked for its run.
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', `${P}/tailoring-runs/run-1`)).toHaveLength(1);
    });
    expect(within(screen.getByRole('main')).getByText(USER_LOADING)).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('a succeeded run: one h1, the status as the history row words it, both documents as text', async () => {
    open();

    expect(await screen.findByText(historyStatusCopy(entry()))).toBeVisible();
    expect(within(screen.getByRole('main')).getAllByRole('heading', { level: 1 })).toHaveLength(1);
    const cv = await section('Tailored CV');
    const letter = await section('Cover letter');
    expect(within(cv).getByText('TAILORED CV BODY')).toBeVisible();
    expect(within(letter).getByText('COVER LETTER BODY')).toBeVisible();
  });

  it("the posting's title links to its posting page", async () => {
    open();

    const link = await screen.findByRole('link', { name: 'Senior Platform Engineer' });
    expect(link).toHaveAttribute('href', `/admin/users/${TARGET_ID}/postings/posting-1`);
  });

  it("the CV's label is shown", async () => {
    open();

    expect(await screen.findByText('Backend roles')).toBeVisible();
  });

  it('a run whose CV was deleted says "CV deleted"', async () => {
    open(world({ cvs: [], historyPages: [[entry({ base_cv: null })]] }), {
      [`GET ${P}/base-cvs/:cvId`]: status(404, 'base_cv_not_found'),
    });

    expect(await screen.findByText(CV_DELETED_NOTE)).toBeVisible();
  });

  it('a failing CV list says "Couldn\'t load the CV." — never "CV deleted" — and the rest renders', async () => {
    open(world(), { [`GET ${P}/base-cvs`]: status(503, 'service_unavailable') });

    expect(await screen.findByText("Couldn't load the CV.", {}, SLOW)).toBeVisible();
    expect(screen.queryByText(CV_DELETED_NOTE)).toBeNull();
    expect(await screen.findByText('TAILORED CV BODY')).toBeVisible();
    expect(screen.getByText(historyStatusCopy(entry()))).toBeVisible();
  });

  it('AC-25: both documents are pre-wrap text nodes — markup and Markdown stay literal', async () => {
    const cvBody = '<img src=x onerror="window.__pwned=1"> and **bold**';
    const letterBody = '<script>window.__pwned=2</script> and _em_';
    const { container } = open(
      world({ runs: [succeededRun({ tailored_cv: cvBody, cover_letter: letterBody })] }),
    );

    const cv = await screen.findByText(cvBody);
    const letter = await screen.findByText(letterBody);
    expect(cv).toHaveClass('whitespace-pre-wrap');
    expect(letter).toHaveClass('whitespace-pre-wrap');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelector('em')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it('run_not_found: "This run no longer exists."', async () => {
    open(world(), { [`GET ${P}/tailoring-runs/:rid`]: status(404, 'tailoring_run_not_found') });

    expect(await screen.findByText(RUN_GONE)).toBeVisible();
  });

  it('a 503 is an alert with Retry', async () => {
    open(world(), { [`GET ${P}/tailoring-runs/:rid`]: status(503, 'service_unavailable') });

    expect(await screen.findByRole('alert', {}, SLOW)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible();
  });
});

describe('queued, running and failed runs — AC-28, OQ-12', () => {
  it.each(['queued', 'running'] as const)(
    'a %s run says "Still running", offers Refresh, and no download buttons',
    async (state) => {
      open(
        world({ runs: [succeededRun({ status: state, tailored_cv: null, cover_letter: null })] }),
      );

      expect(await screen.findByText(RUN_STILL_RUNNING)).toBeVisible();
      expect(screen.getByRole('button', { name: 'Refresh' })).toBeVisible();
      expect(screen.queryByRole('button', { name: /^Download \./ })).not.toBeInTheDocument();
    },
  );

  it('a queued run is not polled; Refresh reads it again', async () => {
    const { fetch } = open(
      world({ runs: [succeededRun({ status: 'queued', tailored_cv: null, cover_letter: null })] }),
    );
    await screen.findByText(RUN_STILL_RUNNING);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(callsTo(fetch, 'GET', `${P}/tailoring-runs/run-1`)).toHaveLength(1);

    await clickWhenEnabled(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', `${P}/tailoring-runs/run-1`)).toHaveLength(2);
    });
  });

  it('a failed run shows its reason as the history row words it and no downloads', async () => {
    const failed = entry({ status: 'failed', failure_reason: 'llm_unavailable' });
    open(
      world({
        runs: [
          succeededRun({
            status: 'failed',
            failure_reason: 'llm_unavailable',
            tailored_cv: null,
            cover_letter: null,
          }),
        ],
        historyPages: [[failed]],
      }),
    );

    expect(await screen.findByText(historyStatusCopy(failed))).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Download \./ })).not.toBeInTheDocument();
  });
});

describe('inline downloads — AC-28', () => {
  it.each([
    ['Tailored CV', 'cv', 'md', 'Download .md'],
    ['Tailored CV', 'cv', 'txt', 'Download .txt'],
    ['Cover letter', 'cover_letter', 'md', 'Download .md'],
    ['Cover letter', 'cover_letter', 'txt', 'Download .txt'],
  ] as const)(
    '%s → %s .%s goes through the client and saveBlob',
    async (heading, doc, fmt, label) => {
      const { fetch } = open();

      const box = await section(heading);
      await clickWhenEnabled(within(box).getByRole('button', { name: label }));

      await waitFor(() => {
        expect(saveBlob).toHaveBeenCalledTimes(1);
      });
      expect(vi.mocked(saveBlob).mock.calls[0]?.[1]).toBe(downloadFilenameFor(doc, fmt));
      const [call] = callsTo(fetch, 'GET', `${P}/tailoring-runs/run-1/documents/${doc}/download`);
      expect(call?.query.get('format')).toBe(fmt);
      expect(call?.authorization).toBe(`Bearer token-${ADMIN.id}`);
    },
  );

  it('a refused download is an alert in place and the button is usable again', async () => {
    open(world(), {
      [`GET ${P}/tailoring-runs/:rid/documents/:kind/download`]: status(
        409,
        'tailoring_run_not_exportable',
      ),
    });

    const box = await section('Tailored CV');
    const button = within(box).getByRole('button', { name: 'Download .md' });
    await clickWhenEnabled(button);

    expect(await within(box).findByRole('alert')).toBeVisible();
    expect(saveBlob).not.toHaveBeenCalled();
    expect(button).toBeEnabled();
  });
});

describe('exports — AC-28', () => {
  const jobs = () => [
    exportJob({ id: 'job-ready', layout_template: 'modern' }),
    exportJob({ id: 'job-old', layout_template: 'formal', current: false }),
    exportJob({
      id: 'job-queued',
      document: 'cover_letter',
      format: 'docx',
      layout_template: null,
      status: 'queued',
      file_url: null,
      byte_size: null,
    }),
    exportJob({
      id: 'job-failed',
      format: 'docx',
      layout_template: null,
      status: 'failed',
      failure_reason: 'render_failed',
      file_url: null,
      byte_size: null,
    }),
  ];
  const DOWNLOAD = /^Download(?! \.)/;

  it('names layout and format; Download only on ready files; "older version" on a stale one', async () => {
    open(world({ exports: { 'run-1': jobs() } }));

    const box = await section('Exports');
    await within(box).findAllByRole('button', { name: DOWNLOAD });
    expect(within(box).getAllByRole('button', { name: DOWNLOAD })).toHaveLength(2);
    expect(within(box).getByText(/modern/i)).toBeVisible();
    expect(within(box).getByText(/formal/i)).toBeVisible();
    expect(within(box).getAllByText(/older version/i)).toHaveLength(1);
    expect(within(box).getByText(/queued/i)).toBeVisible();
    expect(within(box).getByText(/failed/i)).toBeVisible();
  });

  it('a ready file downloads through the client and saveBlob', async () => {
    const { fetch } = open(world({ exports: { 'run-1': [exportJob()] } }));

    const box = await section('Exports');
    await clickWhenEnabled(await within(box).findByRole('button', { name: DOWNLOAD }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledTimes(1);
    });
    expect(vi.mocked(saveBlob).mock.calls[0]?.[1]).toBe(downloadFilenameFor('cv', 'pdf'));
    expect(callsTo(fetch, 'GET', `${P}/export-jobs/job-1/file`)).toHaveLength(1);
  });

  it('410 says "That file is no longer on the server." and the button is usable again', async () => {
    open(world({ exports: { 'run-1': [exportJob()] } }), {
      [`GET ${P}/export-jobs/:jid/file`]: status(410, 'export_file_gone'),
    });

    const box = await section('Exports');
    const button = await within(box).findByRole('button', { name: DOWNLOAD });
    await clickWhenEnabled(button);

    expect(await within(box).findByText(DOWNLOAD_FILE_GONE)).toBeVisible();
    expect(button).toBeEnabled();
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it('no exports says so', async () => {
    open(world({ exports: { 'run-1': [] } }));

    expect(await within(await section('Exports')).findByText('No exports.')).toBeVisible();
  });
});
