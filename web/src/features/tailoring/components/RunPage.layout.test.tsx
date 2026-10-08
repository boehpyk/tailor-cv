import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { makeExportJob } from '@/features/export/test/fixtures';
import { jsonResponse, makeRun, stubWorkspaceFetch } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

/**
 * T24 RED — slice 3.2, AC-30: the layout choice is shared by the CV and cover-letter tabs
 * (plan §0.10: it lives in `RunPage`, which survives the tab switch). Mounted through the real
 * route table, as `RunPage.test.tsx` does.
 */

const RUN_ID = 'run-page-layout-fixture';

function succeededRun(): ReturnType<typeof makeRun> {
  return makeRun({
    id: RUN_ID,
    status: 'succeeded',
    tailored_cv: 'my tailored cv text',
    cover_letter: 'my cover letter text',
    tailored_cv_character_count: 20,
    cover_letter_character_count: 21,
  });
}

function stubRun(exportItems: unknown[] = []): void {
  stubWorkspaceFetch({
    runDetail: { [RUN_ID]: () => Promise.resolve(jsonResponse(200, succeededRun())) },
    exports: { [RUN_ID]: () => Promise.resolve(jsonResponse(200, { items: exportItems })) },
  });
}

describe('RunPage — the PDF layout choice', () => {
  it('survives a switch from the CV tab to the cover-letter tab', async () => {
    stubRun();
    const { router } = renderWithRouter(`/runs/${RUN_ID}/cv`);

    const formal = await screen.findByRole('radio', { name: /Formal/ });
    fireEvent.click(formal);
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /Formal/ })).toBeChecked();
    });

    fireEvent.click(screen.getByRole('tab', { name: /cover letter/i }));

    // Positive control: we really are on the other document now.
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cover_letter`);
    });
    expect(await screen.findByRole('radio', { name: /Formal/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Classic/ })).not.toBeChecked();
  });

  it("pre-selects from the other document's newest PDF job when the page opens", async () => {
    stubRun([
      makeExportJob({
        id: 'letter-pdf',
        tailoring_run_id: RUN_ID,
        document: 'cover_letter',
        format: 'pdf',
        layout_template: 'modern',
        status: 'ready',
        byte_size: 1,
      }),
    ]);
    renderWithRouter(`/runs/${RUN_ID}/cv`);

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /Modern/ })).toBeChecked();
    });
  });
});
