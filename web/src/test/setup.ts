import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// React Testing Library does not unmount between tests on its own under Vitest. Without this, a
// component from a previous test is still in the document and `getByText` finds two matches — which
// presents as a confusing "found multiple elements" failure in a test that is entirely correct.
afterEach(() => {
  cleanup();
});

/**
 * jsdom polyfills ProseMirror needs at mount (F5b, task-list; workspace-progress-and-editor's
 * editor arrives at F8/F9, but the seam is shared test infrastructure so it lives here rather than
 * being duplicated per editor test file).
 *
 * **Measured, not guessed** — task-list F5b says to check by mounting a trivial `useEditor` and
 * deleting the scratch file, which is exactly what was done: a `StarterKit` editor rendered with
 * `@testing-library/react`, then clicked and typed into with `@testing-library/user-event`. Three
 * calls threw `TypeError`s that do not exist in jsdom, all three inside `prosemirror-view`'s own
 * cursor/selection code, none of them ProseMirror bugs — jsdom simply does not implement layout:
 *
 * 1. `Range.prototype.getClientRects` — `prosemirror-view`'s `singleRect` (`coordsAtPos`, used
 *    every time the view scrolls the selection into view) calls it directly.
 * 2. `Range.prototype.getBoundingClientRect` — the fallback `singleRect` takes when a rect list is
 *    empty; jsdom implements this on `Element` but not on `Range`.
 * 3. `document.elementFromPoint` — `posAtCoords`, called on every `mousedown` inside the editor
 *    (i.e. every click), to work out which document position the pointer landed on.
 *
 * Each polyfill returns the cheapest honest answer — an empty rect list, a zero rect, `null` — so a
 * test asserting *layout* (an actual pixel position) would still fail, which is correct: nothing
 * here claims jsdom can lay out text. It only stops ProseMirror's housekeeping from crashing a test
 * that has nothing to do with layout.
 *
 * **Guarded**, so a future jsdom that implements one of these does not silently shadow it with a
 * worse stub — and so a reader who greps for `getClientRects is not a function` (the exact failure
 * a missing guard would reintroduce) finds the reason here instead of rediscovering it.
 */
// `lib.dom.d.ts` declares all three of these as always present, so TypeScript (correctly, for the
// *type*) considers each guard's condition impossible — `@typescript-eslint/no-unnecessary-condition`
// is reading the type, not jsdom's actual implementation, which is exactly the gap being guarded
// against. Silencing the rule here is a **runtime feature-detection**, not a type bug.
// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- jsdom omits this at runtime; see the docstring above
if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = function (): DOMRectList {
    return {
      length: 0,
      item: () => null,
      [Symbol.iterator]: [][Symbol.iterator],
    };
  };
}

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- jsdom omits this at runtime; see the docstring above
if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function (): DOMRect {
    return {
      x: 0,
      y: 0,
      width: 0,
      height: 0,
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      toJSON() {
        return {};
      },
    };
  };
}

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- jsdom omits this at runtime; see the docstring above
if (!document.elementFromPoint) {
  document.elementFromPoint = (): Element | null => null;
}
