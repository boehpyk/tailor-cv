import { describe, expect, it } from 'vitest';

import * as copy from './fitScoreCopy';

/**
 * T42 (`qa`, test-after; AC-47, AC-49). The forbidden-word scan reads the **strings the module
 * exports** (and what its functions return), never the source text, so a comment explaining why
 * "gain" is banned cannot trip it. Word boundaries: "against" and "again" pass.
 */

const FORBIDDEN = [
  'improv\\w*',
  'boost\\w*',
  'increas\\w*',
  'rais(?:e|es|ed|ing)',
  'higher',
  'better',
  'guarantee\\w*',
  'gain\\w*',
  'chance\\w*',
];
const FORBIDDEN_RE = new RegExp(`\\b(?:${FORBIDDEN.join('|')})\\b`, 'i');

/** Every sentence the module can say: its string constants, the records' values, the functions' output. */
function allSentences(): string[] {
  const out: string[] = [];
  for (const value of Object.values(copy)) {
    if (typeof value === 'string') {
      out.push(value);
    } else if (typeof value === 'function') {
      out.push(
        copy.scorePercent(62),
        copy.accountLimit('at 14:00'),
        copy.holdSentence('in 30 seconds'),
        copy.holdSentence(null),
      );
    } else {
      out.push(...Object.values(value as Record<string, string>));
    }
  }
  return out;
}

describe('fitScoreCopy (AC-49: no gain promised)', () => {
  it('scans a real set of sentences', () => {
    expect(allSentences().length).toBeGreaterThan(20);
  });

  it('no sentence the panel says contains a forbidden word', () => {
    expect(allSentences().filter((s) => FORBIDDEN_RE.test(s))).toEqual([]);
  });

  it.each(['gain', 'a better score', 'Improve it', 'raise your odds', 'a high chance', 'Boosted'])(
    'the scan bites: "%s" is caught',
    (s) => {
      expect(FORBIDDEN_RE.test(s)).toBe(true);
    },
  );

  it.each(['against this job', 'check again', 'We delete guest CVs after 24 hours.'])(
    'the scan does not over-fire: "%s" passes',
    (s) => {
      expect(FORBIDDEN_RE.test(s)).toBe(false);
    },
  );

  it('labels the number an estimate', () => {
    expect(copy.ESTIMATE_LABEL).toBe('estimate');
    expect(copy.scorePercent(62)).toBe('62%');
  });
});

describe('fitScoreCopy (AC-47: the notice)', () => {
  it('reads exactly the spec wording', () => {
    expect(copy.FIT_SCORE_NOTICE).toBe(
      'Checking your score sends your CV text and this job posting to Google Gemini. Nothing else is sent.',
    );
  });
});

describe('limitWhen', () => {
  // Local-time constructors: the test holds in any zone.
  const NOW = new Date(2026, 10, 1, 10, 0, 0).getTime(); // Sunday

  it('names the weekday when the instant is two or more days away', () => {
    const at = new Date(2026, 10, 3, 9, 0, 0); // Tuesday
    const day = new Intl.DateTimeFormat(undefined, { weekday: 'long' }).format(at);
    expect(copy.limitWhen(at.toISOString(), NOW)).toMatch(
      new RegExp(`^on ${day} at \\d{1,2}:\\d{2}`),
    );
  });

  it('says "tomorrow" for the next calendar day and nothing for today', () => {
    expect(copy.limitWhen(new Date(2026, 10, 2, 9, 0, 0).toISOString(), NOW)).toMatch(
      /^tomorrow at /,
    );
    expect(copy.limitWhen(new Date(2026, 10, 1, 14, 0, 0).toISOString(), NOW)).toMatch(/^at /);
  });

  it('says "later" for an absent or unreadable instant', () => {
    expect(copy.limitWhen(null, NOW)).toBe('later');
    expect(copy.limitWhen('not a date', NOW)).toBe('later');
  });
});
