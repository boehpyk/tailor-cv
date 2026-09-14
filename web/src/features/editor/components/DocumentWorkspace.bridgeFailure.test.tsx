import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { countCallsTo, makeRun } from '@/test/fixtures';

import { stubDocumentFetch } from '../test/fetchStub';

/**
 * F9 RED — AC-35 / E-29: the run page falls back to the read-only preview when the bridge cannot
 * parse a document, so a succeeded run is never unviewable.
 *
 * **A separate file, on purpose.** `vi.mock` is hoisted to the top of *its own* module, so a
 * per-file mock of `../markdown/bridge` would silently apply to every test in a shared file —
 * including this slice's happy-path tests, which need the *real* bridge once F10a lands. Isolating
 * the throwing mock here is what keeps `DocumentWorkspace.test.tsx`'s other tests honest about which
 * bridge they are exercising.
 *
 * Also why this cannot simply call `createBridge(schema, { parser: throwingParser })` (the seam
 * `bridge.ts`'s own docstring names for AC-35): nothing between here and `DocumentWorkspace` accepts
 * an injected bridge today — `useDocumentEditor`'s F8 skeleton does not call the bridge at all yet
 * (it seeds plain text), so there is no prop or parameter to inject a parser through. Mocking the
 * module is the fallback the task explicitly sanctions for exactly this gap, and it exercises the
 * same failure a real unparseable document would cause once F10b wires the bridge in.
 */
vi.mock('../markdown/bridge', async () => {
  const actual = await vi.importActual<typeof import('../markdown/bridge')>('../markdown/bridge');
  return {
    ...actual,
    parseMarkdown: (): never => {
      throw new Error('boom: this document could not be parsed');
    },
  };
});

// Imported after the mock so the module graph resolves against the throwing `parseMarkdown` above.
const { DocumentWorkspace } = await import('./DocumentWorkspace');

const RUN_ID = 'bridge-failure-fixture-run';

describe('AC-35/E-29 — the bridge throwing falls back to the read-only preview', () => {
  it('shows TailoredDocumentsPreview with "We couldn\'t open this document in the editor.", and issues no PUT', () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
    });
    queryClient.setQueryData(tailoringRunQueryKey(run.id), run);
    const router = createMemoryRouter(
      [{ path: '/runs/:runId/:document', element: <DocumentWorkspace run={run} /> }],
      { initialEntries: [`/runs/${run.id}/cv`] },
    );
    const fetchMock = stubDocumentFetch({});

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("We couldn't open this document in the editor.")).toBeInTheDocument();
    // The fallback IS 1.3's read-only preview, not a bespoke error page — it shows both documents.
    expect(screen.getByText('Tailored CV')).toBeInTheDocument();
    expect(screen.getByText('Cover letter')).toBeInTheDocument();
    expect(countCallsTo(fetchMock, `/api/tailoring-runs/${RUN_ID}/documents/cv`, 'PUT')).toBe(0);
  });
});
