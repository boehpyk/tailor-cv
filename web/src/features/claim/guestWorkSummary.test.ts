import { describe, expect, it } from 'vitest';

import { guestCv, guestRun, workingCopy } from './test/support';
import { summarizeGuestWork } from './guestWorkSummary';

/**
 * T30 RED — `summarizeGuestWork` (AC-37), pure. Against T29's skeleton it returns a sentinel
 * (`runCount: -1`, non-null even for empty lists), so every case below is red on its assertion.
 *
 * The rule, from the spec: the offer names each CV that is **not a working copy** by
 * `original_filename`, and the number of runs; `null` iff there is no such CV and no run.
 */
describe('summarizeGuestWork', () => {
  it('is null when the guest holds nothing at all', () => {
    expect(summarizeGuestWork([], [])).toBeNull();
  });

  it('names a lone CV and counts zero runs', () => {
    expect(summarizeGuestWork([guestCv({ original_filename: 'jane.pdf' })], [])).toEqual({
      cvFilenames: ['jane.pdf'],
      runCount: 0,
    });
  });

  it('counts runs when there is no CV at all', () => {
    expect(summarizeGuestWork([], [guestRun({ id: 'r1' }), guestRun({ id: 'r2' })])).toEqual({
      cvFilenames: [],
      runCount: 2,
    });
  });

  it('keeps list order for several CVs', () => {
    const cvs = [
      guestCv({ id: 'a', original_filename: 'b-second-uploaded.pdf' }),
      guestCv({ id: 'b', original_filename: 'a-first-uploaded.docx' }),
    ];

    expect(summarizeGuestWork(cvs, [])?.cvFilenames).toEqual([
      'b-second-uploaded.pdf',
      'a-first-uploaded.docx',
    ]);
  });

  it('leaves a working copy out of the filenames (a claim never moves one)', () => {
    const cvs = [guestCv({ original_filename: 'jane.pdf' }), workingCopy()];

    expect(summarizeGuestWork(cvs, [])).toEqual({ cvFilenames: ['jane.pdf'], runCount: 0 });
  });

  it('is null when the only CV is a working copy and there are no runs', () => {
    expect(summarizeGuestWork([workingCopy()], [])).toBeNull();
  });

  it('is not null for a working copy plus a run — the run is still the guest’s work', () => {
    expect(summarizeGuestWork([workingCopy()], [guestRun()])).toEqual({
      cvFilenames: [],
      runCount: 1,
    });
  });

  it('counts every run whatever its status (a failed run is still moved)', () => {
    const runs = [
      guestRun({ id: 'ok', status: 'succeeded' }),
      guestRun({ id: 'bad', status: 'failed' }),
      guestRun({ id: 'busy', status: 'running' }),
    ];

    expect(summarizeGuestWork([], runs)?.runCount).toBe(3);
  });

  it('counts a CV whose extraction failed (it is the guest’s, and deletable)', () => {
    const failed = guestCv({
      original_filename: 'scan.pdf',
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'no_text_layer',
    });

    expect(summarizeGuestWork([failed], [])?.cvFilenames).toEqual(['scan.pdf']);
  });
});
