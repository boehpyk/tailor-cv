import { describe, expect, it } from 'vitest';

import { claimResult } from './test/support';
import { claimSuccessNote, guestWorkOfferSentence } from './claimCopy';

/**
 * T30 RED — the two sentences built from counts (AC-37, AC-38). Against T29's skeleton both return
 * `''`. The one example the spec writes out is pinned verbatim; the plural rules are the ordinary
 * ones, pinned both ways so a hard-coded "s" fails.
 */
describe('claimSuccessNote (AC-38)', () => {
  it('says what the spec says for 1 CV and 2 tailored applications', () => {
    expect(claimSuccessNote(claimResult({ base_cvs: 1, tailoring_runs: 2 }))).toBe(
      'Kept in your account: 1 CV, 2 tailored applications.',
    );
  });

  it('pluralizes CVs and singularizes one application', () => {
    expect(claimSuccessNote(claimResult({ base_cvs: 2, tailoring_runs: 1 }))).toBe(
      'Kept in your account: 2 CVs, 1 tailored application.',
    );
  });

  it('reads the server’s counts, never the postings or exports', () => {
    const note = claimSuccessNote(
      claimResult({ base_cvs: 1, tailoring_runs: 2, job_postings: 9, export_jobs: 8 }),
    );

    expect(note).toBe('Kept in your account: 1 CV, 2 tailored applications.');
  });

  it('omits a part that moved nothing instead of printing "0"', () => {
    const note = claimSuccessNote(claimResult({ base_cvs: 0, tailoring_runs: 3 }));

    expect(note).toContain('3 tailored applications');
    expect(note).not.toMatch(/\b0\b/);
  });
});

describe('guestWorkOfferSentence (AC-37)', () => {
  it('names each CV by filename and the number of tailored applications', () => {
    const sentence = guestWorkOfferSentence({
      cvFilenames: ['jane.pdf', 'sam.docx'],
      runCount: 2,
    });

    expect(sentence).toContain('jane.pdf');
    expect(sentence).toContain('sam.docx');
    expect(sentence).toContain('2 tailored applications');
  });

  it('singularizes one application', () => {
    const sentence = guestWorkOfferSentence({ cvFilenames: ['jane.pdf'], runCount: 1 });

    expect(sentence).toContain('1 tailored application');
    expect(sentence).not.toContain('1 tailored applications');
  });

  it('is not empty for work that is only runs', () => {
    expect(guestWorkOfferSentence({ cvFilenames: [], runCount: 3 })).toContain(
      '3 tailored applications',
    );
  });
});
