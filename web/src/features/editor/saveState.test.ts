import { describe, expect, it } from 'vitest';

import { saveStateCopy } from './saveState';

/**
 * F9 RED — the eight save-state strings (feature-spec AC-31: "the save state is visible at all
 * times: Saved · Unsaved changes · Saving… · Couldn't save (+ Retry) · Changed elsewhere · Saving
 * paused · Session expired · Can't save: <reason>").
 *
 * Written against the **spec**, not `saveState.ts`'s F8 skeleton, whose `saveStateCopy` is a
 * typed map of eight empty strings — so every non-emptiness assertion below fails on a real empty
 * string, never on an `ImportError`. F10c is expected to fill the map without touching this file.
 *
 * The rule this guards is AC-31's "still working vs failed" distinction, applied to saves: a user
 * who cannot tell *Saving…* from *Couldn't save* will refresh mid-save and either lose the edit or
 * pay for nothing (Constitution, "React" conventions).
 */
describe('saveStateCopy (AC-31)', () => {
  const kinds = Object.keys(saveStateCopy) as (keyof typeof saveStateCopy)[];

  it.each(kinds)('has a non-empty sentence for "%s"', (kind) => {
    expect(saveStateCopy[kind]).not.toBe('');
  });

  it('has eight distinct entries — one per state in the union, no two sharing a sentence', () => {
    const sentences = kinds.map((kind) => saveStateCopy[kind]);
    expect(kinds).toHaveLength(8);
    expect(new Set(sentences).size).toBe(8);
  });

  it('"Saving…" is never a substring of "Couldn\'t save", and vice versa (AC-31\'s "still working vs failed" rule)', () => {
    expect(saveStateCopy.saving).not.toContain(saveStateCopy.failed);
    expect(saveStateCopy.failed).not.toContain(saveStateCopy.saving);
  });
});
