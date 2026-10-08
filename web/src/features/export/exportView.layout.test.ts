import { describe, expect, it } from 'vitest';

import { defaultLayoutFor, latestExportJobFor, viewOfExport } from './exportView';
import { makeExportJob, makeExportMutations } from './test/fixtures';

import type { ExportTarget } from './exportView';

/**
 * T24 RED — slice 3.2, AC-30 (pre-selection is derived) and AC-31 (a control is about one layout).
 *
 * Written from the spec. `defaultLayoutFor` direct tests are the only ones in this slice that may
 * go red on the skeleton's thrown `not implemented`: a throwing stub cannot return a layout, so the
 * assertion is never reached. The matching tests go red on a real assertion.
 */

const NOW_MS = Date.parse('2026-09-18T10:00:05.000Z');

function pdfTarget(
  layout: 'classic' | 'modern' | 'formal',
  document = 'cv' as const,
): ExportTarget {
  return { document, format: 'pdf', layoutTemplate: layout };
}

describe('defaultLayoutFor (AC-30)', () => {
  it('is null when the run has no export job at all', () => {
    expect(defaultLayoutFor([])).toBeNull();
  });

  it('is the layout of the newest PDF job (the list is newest-first)', () => {
    const jobs = [
      makeExportJob({ id: 'newest', layout_template: 'formal' }),
      makeExportJob({ id: 'older', layout_template: 'modern' }),
    ];
    expect(defaultLayoutFor(jobs)).toBe('formal');
  });

  it('looks across both documents: a newer cover-letter PDF wins over an older CV PDF', () => {
    const jobs = [
      makeExportJob({ id: 'letter', document: 'cover_letter', layout_template: 'modern' }),
      makeExportJob({ id: 'cv', document: 'cv', layout_template: 'formal' }),
    ];
    expect(defaultLayoutFor(jobs)).toBe('modern');
  });

  it('ignores a newer DOCX job, which has no layout', () => {
    const jobs = [
      makeExportJob({ id: 'word', format: 'docx' }),
      makeExportJob({ id: 'pdf', layout_template: 'modern' }),
    ];
    expect(defaultLayoutFor(jobs)).toBe('modern');
  });

  it('is null when the only jobs are DOCX', () => {
    expect(defaultLayoutFor([makeExportJob({ format: 'docx' })])).toBeNull();
  });

  it('counts a failed PDF job: the layout was still the last one requested', () => {
    const jobs = [
      makeExportJob({
        id: 'failed',
        layout_template: 'formal',
        status: 'failed',
        failure_reason: 'render_failed',
      }),
      makeExportJob({ id: 'ready', layout_template: 'modern', status: 'ready', byte_size: 1 }),
    ];
    expect(defaultLayoutFor(jobs)).toBe('formal');
  });
});

describe('a PDF control is about one (document, layout) (AC-31)', () => {
  const jobs = [
    makeExportJob({ id: 'modern-job', layout_template: 'modern', status: 'ready', byte_size: 5 }),
    makeExportJob({ id: 'classic-job', layout_template: 'classic', status: 'queued' }),
  ];

  it('latestExportJobFor returns the job of the target layout, not merely the newest PDF', () => {
    expect(latestExportJobFor(pdfTarget('classic'), jobs)?.id).toBe('classic-job');
    expect(latestExportJobFor(pdfTarget('modern'), jobs)?.id).toBe('modern-job');
  });

  it('latestExportJobFor finds nothing for a layout that was never requested', () => {
    expect(latestExportJobFor(pdfTarget('formal'), jobs)).toBeUndefined();
  });

  it('viewOfExport shows each layout its own state from the same list', () => {
    const modern = viewOfExport(pdfTarget('modern'), jobs, makeExportMutations(), NOW_MS);
    const classic = viewOfExport(pdfTarget('classic'), jobs, makeExportMutations(), NOW_MS);
    const formal = viewOfExport(pdfTarget('formal'), jobs, makeExportMutations(), NOW_MS);

    expect(modern.kind).toBe('ready');
    expect(classic.kind).toBe('queued');
    expect(formal.kind).toBe('idle');
  });

  it("another layout's in-flight POST does not put this layout into 'requesting'", () => {
    const mutations = makeExportMutations({ requesting: pdfTarget('modern') });

    expect(viewOfExport(pdfTarget('modern'), [], mutations, NOW_MS).kind).toBe('requesting');
    expect(viewOfExport(pdfTarget('formal'), [], mutations, NOW_MS).kind).toBe('idle');
  });

  it("another layout's download failure is not shown on this layout", () => {
    const mutations = makeExportMutations({
      downloadFailure: { target: pdfTarget('modern'), error: new Error('boom') },
    });

    expect(viewOfExport(pdfTarget('modern'), jobs, mutations, NOW_MS).kind).toBe('downloadFailed');
    expect(viewOfExport(pdfTarget('classic'), jobs, mutations, NOW_MS).kind).toBe('queued');
  });

  it('a DOCX target (layout null) still finds the DOCX job whatever PDF layouts exist', () => {
    const docx = makeExportJob({ id: 'word', format: 'docx', status: 'queued' });
    const target: ExportTarget = { document: 'cv', format: 'docx', layoutTemplate: null };

    expect(latestExportJobFor(target, [...jobs, docx])?.id).toBe('word');
  });
});
