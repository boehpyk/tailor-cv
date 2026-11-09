import { describe, expect, it } from 'vitest';

import * as adminCopy from '@/features/admin/adminCopy';
import * as fitScoreCopy from '@/features/scoring/fitScoreCopy';
import * as marketingCopy from '@/features/marketing/marketingCopy';

import * as billingCopy from './billingCopy';

/**
 * T37 (`qa`, test-after; AC-37). Slice 5.2's word scans, extending 5.1's AC-49 scan
 * (`fitScoreCopy.test.ts`) the way it is built: they read the **strings the modules export** (and
 * what their functions return), never the source, so a comment explaining why a word is banned
 * cannot trip them. Word boundaries: "tokenize" is not "token" is not asked of anyone; "against"
 * and "again" pass.
 *
 * Banned in the slice's copy: *token* for a credit; and the gain promises *boost*, *raise*,
 * *increase*, *improve your score*, *guarantee*.
 */

const BANNED = [
  'tokens?',
  'boost\\w*',
  'rais(?:e|es|ed|ing)',
  'increas\\w*',
  'improv\\w* your score',
  'guarantee\\w*',
];
const BANNED_RE = new RegExp(`\\b(?:${BANNED.join('|')})\\b`, 'i');

/** Every string reachable from a value: constants, record values, array items, nested objects. */
function strings(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (typeof value === 'object' && value !== null) return Object.values(value).flatMap(strings);
  return [];
}

const COUNTS = {
  base_cvs: 1,
  files: 2,
  logins: 3,
  tailoring_runs: 4,
  job_postings: 5,
  export_jobs: 6,
  tracked_applications: 7,
  fit_scores: 8,
  credits_left: 9,
  credit_entries: 10,
};

const billingSentences = [
  ...strings(Object.values(billingCopy).filter((v) => typeof v !== 'function')),
  billingCopy.creditsLine(3),
  billingCopy.creditsLine(0),
  billingCopy.packLabel(3, 500),
  billingCopy.packLabel(10, 1250),
  billingCopy.optInLabel('a@example.com'),
  billingCopy.balanceLine(1),
  billingCopy.balanceLine(3),
  billingCopy.signedCredits(-1),
  billingCopy.signedCredits(2),
  billingCopy.unusedCreditsWarning(1),
  billingCopy.unusedCreditsWarning(2),
];

const COMPARISON_KEYS = [
  'COMPARE_HEADING',
  'COMPARE_BUTTON',
  'COMPARE_NOTICE',
  'SIDE_LABELS',
  'DRAFT_NOTE',
  'SIDE_SCORING',
  'SIDE_FAILED',
  'COMPARE_BUSY',
  'COMPARE_UNAVAILABLE',
  'NOT_COMPARABLE',
  'VERDICTS',
] as const;
const comparisonSentences = [
  ...strings(COMPARISON_KEYS.map((key) => fitScoreCopy[key])),
  fitScoreCopy.deltaPoints(8),
  fitScoreCopy.deltaPoints(-3),
  fitScoreCopy.deltaPoints(0),
  fitScoreCopy.deltaPoints(1),
];

const demandSentences = strings(
  Object.entries(adminCopy)
    .filter(([name]) => name.startsWith('DEMAND_'))
    .map(([, value]) => value),
);
const eraseSentences = [
  adminCopy.eraseDialogMessage('ann@example.com', COUNTS, false),
  adminCopy.eraseDialogMessage('ann@example.com', { ...COUNTS, credits_left: 1 }, true),
];

const marketingSentences = strings(
  Object.values(marketingCopy).filter((v) => typeof v !== 'function'),
);

const SURFACES: readonly (readonly [string, readonly string[], number])[] = [
  ['billingCopy.ts', billingSentences, 30],
  ['the comparison block of fitScoreCopy.ts', comparisonSentences, 10],
  ['the demand copy in adminCopy.ts', [...demandSentences, ...eraseSentences], 15],
  ['marketingCopy.ts', marketingSentences, 40],
];

describe('AC-37: no token, no promised gain', () => {
  it.each(SURFACES)('%s: scans a real set of sentences', (_name, sentences, atLeast) => {
    expect(sentences.length).toBeGreaterThanOrEqual(atLeast);
  });

  it.each(SURFACES)('%s: no sentence contains a banned word', (_name, sentences) => {
    expect(sentences.filter((s) => BANNED_RE.test(s))).toEqual([]);
  });

  it.each([
    '3 tokens left',
    'Buy a token',
    'Boost your odds',
    'boosted',
    'raise your chances',
    'raising it',
    'Increase your fit',
    'improves your score',
    'Improve your score fast',
    'We guarantee a match',
    'Guaranteed',
  ])('the scan bites: "%s" is caught', (s) => {
    expect(BANNED_RE.test(s)).toBe(true);
  });

  it.each([
    'Uses 1 credit · You have 3',
    'against this job',
    'check again',
    'An upraised flag',
    'tokenization is not asked of anyone',
    'Improve this sentence',
  ])('the scan does not over-fire: "%s" passes', (s) => {
    expect(BANNED_RE.test(s)).toBe(false);
  });
});
