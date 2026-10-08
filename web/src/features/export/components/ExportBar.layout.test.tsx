import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { LAYOUT_TEMPLATES } from '../layouts';
import { blobResponse, stubExportFetch } from '../test/fetchStub';
import { EXPORT_RUN_ID, makeExportJob } from '../test/fixtures';
import { ExportBar } from './ExportBar';

import type { SaveState } from '@/features/editor/saveState';
import type { ExportJob, LayoutTemplate } from '../types';
import type { ReactNode } from 'react';

/**
 * T24 RED — slice 3.2, AC-30…AC-33 for `ExportBar` and the picker it renders.
 *
 * Written from feature-spec AC-30…AC-33 and technical-plan §7, not from the T23 skeleton (a
 * fieldset and a legend, no radios; no `layout_template` in any body). Expected strings are
 * literals from the spec; layout names/descriptions come from `LAYOUT_TEMPLATES`, as the task says.
 *
 * Idle PDF control is the button named `PDF` (1.5's label, unchanged); the ready one is
 * `Download PDF — <Name> layout` (AC-31), matched as a prefix so the optional size suffix is free.
 *
 * The queryClient uses `gcTime: Infinity`: an absence assertion on a cache that `gcTime: 0` may
 * have collected tests the collector, not the code (2.1's trap).
 */

const SAVED: SaveState = { kind: 'saved' };

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
}

function renderBar(
  overrides: { layout?: LayoutTemplate | null; document?: 'cv' | 'cover_letter' } = {},
  onLayoutChange: (layout: LayoutTemplate) => void = () => undefined,
): ReturnType<typeof render> {
  const client = makeQueryClient();
  function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return render(
    <ExportBar
      runId={EXPORT_RUN_ID}
      document={overrides.document ?? 'cv'}
      saveState={SAVED}
      layout={overrides.layout ?? null}
      onLayoutChange={onLayoutChange}
    />,
    { wrapper: Wrapper },
  );
}

/**
 * `ExportBar` with the state `RunPage` normally holds, so a click on a radio really changes what
 * the bar shows. (The bar itself is controlled: it renders `layout ?? derived`.)
 */
function StatefulBar(): React.JSX.Element {
  const [client] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>
  );
}

function Harness(): React.JSX.Element {
  const [layout, setLayout] = useState<LayoutTemplate | null>(null);
  return (
    <ExportBar
      runId={EXPORT_RUN_ID}
      document="cv"
      saveState={SAVED}
      layout={layout}
      onLayoutChange={setLayout}
    />
  );
}

function stubJobs(items: readonly ExportJob[]): ReturnType<typeof stubExportFetch> {
  return stubExportFetch(EXPORT_RUN_ID, {
    exportJobs: () => Promise.resolve(jsonResponse(200, { items })),
    requestExport: () =>
      Promise.resolve(jsonResponse(202, makeExportJob({ id: 'new-job', status: 'queued' }))),
    downloadDocument: {
      'cv:md': () => Promise.resolve(blobResponse(200, 'text/markdown')),
      'cv:txt': () => Promise.resolve(blobResponse(200, 'text/plain')),
    },
  });
}

function pdfJob(id: string, layout: LayoutTemplate, overrides: Partial<ExportJob> = {}): ExportJob {
  return makeExportJob({ id, format: 'pdf', layout_template: layout, ...overrides });
}

function radio(name: RegExp): HTMLInputElement {
  return screen.getByRole<HTMLInputElement>('radio', { name });
}

function postBodies(fetchMock: ReturnType<typeof vi.fn>): Array<Record<string, unknown>> {
  return fetchMock.mock.calls
    .filter((call) => (call as [unknown, RequestInit | undefined])[1]?.method === 'POST')
    .map(
      (call) =>
        JSON.parse((call as [unknown, { body: string }])[1].body) as Record<string, unknown>,
    );
}

function stubObjectUrl(): void {
  URL.createObjectURL = vi.fn(() => 'blob:mock-url');
  URL.revokeObjectURL = vi.fn();
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('AC-30 — the picker', () => {
  it('renders a "PDF layout" group with three native radios named by LAYOUT_TEMPLATES, in order', async () => {
    stubJobs([]);
    renderBar();

    expect(await screen.findByRole('group', { name: 'PDF layout' })).toBeInTheDocument();
    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(3);
    LAYOUT_TEMPLATES.forEach((template, index) => {
      expect(radios[index]).toHaveAccessibleName(new RegExp(template.name));
      expect(screen.getAllByText(template.description).length).toBeGreaterThan(0);
    });
  });

  it('pre-selects Classic when the run has no PDF job', async () => {
    stubJobs([]);
    renderBar();

    await waitFor(() => {
      expect(radio(/Classic/)).toBeChecked();
    });
    expect(radio(/Modern/)).not.toBeChecked();
    expect(radio(/Formal/)).not.toBeChecked();
  });

  it("pre-selects the layout of the run's newest PDF job, of either document", async () => {
    stubJobs([
      pdfJob('letter', 'modern', { document: 'cover_letter', status: 'ready', byte_size: 1 }),
      pdfJob('cv', 'formal', { status: 'ready', byte_size: 1 }),
    ]);
    renderBar({ document: 'cv' });

    await waitFor(() => {
      expect(radio(/Modern/)).toBeChecked();
    });
    expect(radio(/Classic/)).not.toBeChecked();
  });

  it('ignores a newer DOCX job when pre-selecting', async () => {
    stubJobs([makeExportJob({ id: 'word', format: 'docx' }), pdfJob('pdf', 'formal')]);
    renderBar();

    await waitFor(() => {
      expect(radio(/Formal/)).toBeChecked();
    });
  });

  it("a user's choice overrides the derived pre-selection and is reported once", async () => {
    stubJobs([pdfJob('pdf', 'formal')]);
    const onLayoutChange = vi.fn();
    renderBar({}, onLayoutChange);
    await waitFor(() => {
      expect(radio(/Formal/)).toBeChecked();
    });

    fireEvent.click(radio(/Modern/));

    expect(onLayoutChange).toHaveBeenCalledTimes(1);
    expect(onLayoutChange).toHaveBeenCalledWith('modern');
  });

  it('shows the layout the owner of the state passes in, over the derived one', async () => {
    stubJobs([pdfJob('pdf', 'formal')]);
    renderBar({ layout: 'modern' });

    await waitFor(() => {
      expect(radio(/Modern/)).toBeChecked();
    });
    expect(radio(/Formal/)).not.toBeChecked();
  });

  it('writes nothing to browser storage when a layout is chosen', async () => {
    stubJobs([]);
    render(<StatefulBar />);
    await waitFor(() => {
      expect(radio(/Classic/)).toBeChecked();
    });
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    localStorage.clear();
    sessionStorage.clear();

    fireEvent.click(radio(/Formal/));

    // Positive control: the choice really took effect, so the absence below is not an idle bar.
    await waitFor(() => {
      expect(radio(/Formal/)).toBeChecked();
    });
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });
});

describe('AC-31 — the PDF control follows the selected layout', () => {
  const pdfButton = (): HTMLElement =>
    screen.getByRole('button', { name: /^(PDF|Download PDF|Export again)/ });

  it('no job for the selected layout: the idle PDF control, enabled', async () => {
    stubJobs([pdfJob('c1', 'classic', { status: 'ready', byte_size: 1 })]);
    renderBar({ layout: 'modern' });

    expect(await screen.findByRole('button', { name: 'PDF' })).toBeEnabled();
  });

  it('a ready, current job for the layout: "Download PDF — Modern layout"', async () => {
    stubJobs([pdfJob('m1', 'modern', { status: 'ready', byte_size: 2048 })]);
    renderBar({ layout: 'modern' });

    expect(
      await screen.findByRole('button', { name: /^Download PDF — Modern layout/ }),
    ).toBeEnabled();
  });

  it('a ready, stale job for the layout: "Your document changed — Export again"', async () => {
    stubJobs([pdfJob('f1', 'formal', { status: 'ready', byte_size: 1, current: false })]);
    renderBar({ layout: 'formal' });

    expect(await screen.findByText('Your document changed — Export again')).toBeInTheDocument();
  });

  it("a failed job for the layout shows 1.5's notice and, when retryable, Export again", async () => {
    stubJobs([
      pdfJob('c1', 'classic', {
        status: 'failed',
        failure_reason: 'render_timed_out',
        retryable: true,
      }),
    ]);
    renderBar({ layout: 'classic' });

    expect(await screen.findByText('That took too long.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /export again/i })).toBeInTheDocument();
  });

  it('a queued job for the layout: "Waiting for a worker…" and the PDF control is disabled', async () => {
    stubJobs([pdfJob('q1', 'modern', { status: 'queued' })]);
    renderBar({ layout: 'modern' });

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'PDF' })).toBeDisabled();
  });

  it('a rendering job for the layout: "Preparing your PDF…"', async () => {
    stubJobs([
      pdfJob('r1', 'formal', {
        status: 'rendering',
        started_at: '2026-09-18T10:00:00.000Z',
      }),
    ]);
    renderBar({ layout: 'formal' });

    expect(await screen.findByText(/^Preparing your PDF… \d+s$/)).toBeInTheDocument();
  });

  it("switching layout shows each layout's own state and never POSTs, cancels or hides another's", async () => {
    const fetchMock = stubJobs([
      pdfJob('m1', 'modern', { status: 'queued' }),
      pdfJob('f1', 'formal', { status: 'ready', byte_size: 1 }),
      pdfJob('c1', 'classic', {
        status: 'failed',
        failure_reason: 'render_timed_out',
        retryable: true,
      }),
    ]);
    render(<StatefulBar />);
    // Newest PDF is modern's queued job -> pre-selected, and its progress is on screen.
    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(radio(/Modern/)).toBeChecked();
    fetchMock.mockClear();

    fireEvent.click(radio(/Formal/));
    expect(
      await screen.findByRole('button', { name: /^Download PDF — Formal layout/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText('Waiting for a worker…')).not.toBeInTheDocument();

    fireEvent.click(radio(/Classic/));
    expect(await screen.findByText('That took too long.')).toBeInTheDocument();

    fireEvent.click(radio(/Modern/));
    // Switching back: the first job is still where it was.
    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();

    expect(postBodies(fetchMock)).toEqual([]);
    expect(pdfButton()).toBeDisabled();
  });
});

describe('AC-32 — loading, error, empty, success', () => {
  it('while the export list loads: the picker is disabled and aria-busy, Classic is selected', () => {
    stubExportFetch(EXPORT_RUN_ID, { exportJobs: () => new Promise<Response>(() => undefined) });
    renderBar();

    const group = screen.getByRole('group', { name: 'PDF layout' });
    expect(group).toHaveAttribute('aria-busy', 'true');
    expect(group).toBeDisabled();
    expect(radio(/Classic/)).toBeChecked();
    expect(radio(/Classic/)).toBeDisabled();
    expect(screen.getByRole('button', { name: 'PDF' })).toBeDisabled();
  });

  it('once loaded the picker is enabled and no longer busy', async () => {
    stubJobs([]);
    renderBar();

    await waitFor(() => {
      expect(screen.getByRole('group', { name: 'PDF layout' })).toHaveAttribute(
        'aria-busy',
        'false',
      );
    });
    expect(screen.getByRole('group', { name: 'PDF layout' })).toBeEnabled();
    expect(radio(/Classic/)).toBeEnabled();
  });

  it('does not jump under the pointer: Classic shown while loading, then the derived layout once it arrives', async () => {
    let resolveJobs: (response: Response) => void = () => undefined;
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        new Promise<Response>((resolve) => {
          resolveJobs = resolve;
        }),
    });
    renderBar();
    expect(radio(/Classic/)).toBeChecked();

    resolveJobs(jsonResponse(200, { items: [pdfJob('f1', 'formal')] }));

    await waitFor(() => {
      expect(radio(/Formal/)).toBeChecked();
    });
  });

  it('on a list error the picker stays usable and the PDF control is disabled with a retry', async () => {
    vi.useFakeTimers();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.reject(new TypeError('Failed to fetch')),
    });
    renderBar();
    for (const ms of [0, 1000, 2000, 4000, 1000]) {
      await vi.advanceTimersByTimeAsync(ms);
    }

    expect(screen.getByText(/we couldn't check your downloads/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check again/i })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'PDF layout' })).toBeEnabled();
    expect(radio(/Modern/)).toBeEnabled();
    expect(screen.getByRole('button', { name: 'PDF' })).toBeDisabled();
  });

  it('empty: Classic selected and Export PDF available', async () => {
    stubJobs([]);
    renderBar();

    expect(await screen.findByRole('button', { name: 'PDF' })).toBeEnabled();
    expect(radio(/Classic/)).toBeChecked();
  });

  it('success: a ready PDF for the selected layout offers Download PDF', async () => {
    stubJobs([pdfJob('c1', 'classic', { status: 'ready', byte_size: 1 })]);
    renderBar();

    expect(
      await screen.findByRole('button', { name: /^Download PDF — Classic layout/ }),
    ).toBeEnabled();
  });

  it('a preview that fails to load disappears, keeping the name and description (no broken-image icon)', async () => {
    stubJobs([]);
    const { container } = renderBar();
    await waitFor(() => {
      expect(radio(/Classic/)).toBeEnabled();
    });
    const images = Array.from(container.querySelectorAll('img'));
    // Positive control: three previews are rendered before any failure.
    expect(images).toHaveLength(3);
    const classicCard = radio(/Classic/).closest('label') ?? radio(/Classic/).parentElement;
    const classicImage = classicCard?.querySelector('img');
    expect(classicImage).toBeTruthy();

    fireEvent.error(classicImage as HTMLImageElement);

    await waitFor(() => {
      const still = classicCard?.querySelector('img') ?? null;
      expect(still === null || still.hidden || still.style.display === 'none').toBe(true);
    });
    expect(screen.getAllByText('Classic').length).toBeGreaterThan(0);
    expect(screen.getByText('Clean and familiar')).toBeInTheDocument();
    // The other two previews are untouched.
    expect(Array.from(container.querySelectorAll('img')).filter((i) => !i.hidden)).toHaveLength(2);
  });
});

describe('AC-33 — the request body', () => {
  it('a PDF request carries the selected layout_template', async () => {
    const fetchMock = stubJobs([]);
    renderBar({ layout: 'formal' });
    fireEvent.click(await screen.findByRole('button', { name: 'PDF' }));

    await waitFor(() => {
      expect(postBodies(fetchMock)).toHaveLength(1);
    });
    expect(postBodies(fetchMock)[0]).toEqual({
      document: 'cv',
      format: 'pdf',
      layout_template: 'formal',
    });
  });

  it('a PDF request with no explicit choice sends the derived layout, never omits it', async () => {
    const fetchMock = stubJobs([
      pdfJob('letter', 'modern', { document: 'cover_letter', status: 'ready', byte_size: 1 }),
    ]);
    renderBar({ document: 'cv' });
    await waitFor(() => {
      expect(radio(/Modern/)).toBeChecked();
    });
    fireEvent.click(await screen.findByRole('button', { name: 'PDF' }));

    await waitFor(() => {
      expect(postBodies(fetchMock)).toHaveLength(1);
    });
    expect(postBodies(fetchMock)[0]).toMatchObject({ format: 'pdf', layout_template: 'modern' });
  });

  it('a DOCX request never carries layout_template, even with a layout selected', async () => {
    const fetchMock = stubJobs([]);
    renderBar({ layout: 'formal' });
    // Positive control first: the PDF request does carry it, so the absence below can fail.
    fireEvent.click(await screen.findByRole('button', { name: 'PDF' }));
    await waitFor(() => {
      expect(postBodies(fetchMock)).toHaveLength(1);
    });
    expect(postBodies(fetchMock)[0]).toHaveProperty('layout_template', 'formal');
    fetchMock.mockClear();

    fireEvent.click(screen.getByRole('button', { name: 'Word' }));

    await waitFor(() => {
      expect(postBodies(fetchMock)).toHaveLength(1);
    });
    const body = postBodies(fetchMock)[0];
    expect(body).toMatchObject({ document: 'cv', format: 'docx' });
    expect(body).not.toHaveProperty('layout_template');
  });

  it('Markdown and plain-text downloads are GETs with no layout parameter', async () => {
    stubObjectUrl();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const fetchMock = stubJobs([]);
    renderBar({ layout: 'formal' });
    fireEvent.click(await screen.findByRole('button', { name: 'Markdown' }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('format=md'))).toBe(true);
    });
    fireEvent.click(screen.getByRole('button', { name: 'Plain text' }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('format=txt'))).toBe(true);
    });

    const downloads = fetchMock.mock.calls.filter((c) => String(c[0]).includes('/download?'));
    expect(downloads).toHaveLength(2);
    for (const call of downloads) {
      expect(String(call[0])).not.toMatch(/layout/i);
      expect((call[1] as RequestInit | undefined)?.method ?? 'GET').toBe('GET');
    }
    expect(postBodies(fetchMock)).toEqual([]);
  });
});
