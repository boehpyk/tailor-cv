/**
 * One format's control — **SKELETON (F4). It does nothing, on purpose.**
 *
 * What ships here is a `<button>` carrying the format's label and, for a queued format, an empty
 * `role="status"` region beside it. No view, no handlers, no disabled state, no copy beyond the
 * label. F6 makes it work.
 *
 * ## Why a deliberately broken component is the point
 *
 * A skeleton exists so that the test written against it fails **on its assertion** rather than on
 * "unable to find an element with the role button" (docs/sdlc.md §2). An `ImportError`-shaped red
 * proves a file is absent; it proves nothing about whether the assertion discriminates. This
 * project has now paid for that lesson four times — **1.1's T33/T34, 1.3's T40, 1.4's F5 and F8**
 * each shipped a frontend skeleton that already worked, so the tests written against it passed the
 * moment they arrived and nobody ever saw them fail. A test that has never been observed failing
 * is a test nobody has checked.
 *
 * So the roles are real and the behaviour is absent. F5's table over every view state will red on
 * every row, and F6 is where all of it gets implemented.
 */

import { EXPORT_FORMAT_LABELS } from '../exportCopy';

import type { ExportFormat } from '../types';

export interface ExportControlProps {
  readonly format: ExportFormat;
  /**
   * Whether this format is rendered by a worker (`queued` — a job to poll) or inside the request
   * (`inline` — bytes back, no job, no row). **Passed in rather than derived from `format`**: the
   * split is a fact of the API's two endpoints, already stated by `QueuedExportFormat` /
   * `InlineExportFormat`, and a `format === 'pdf' || format === 'docx'` here would be a second copy
   * of it free to disagree with the first.
   *
   * It is what decides the roles: AC-36 asks for a `<button>` per format and a `role="status"` per
   * **queued** format's state, because an inline format has no server-side lifecycle to announce —
   * its three states are all facts about this browser's own download.
   */
  readonly delivery: 'queued' | 'inline';
}

export function ExportControl({ format, delivery }: ExportControlProps): React.JSX.Element {
  return (
    <div>
      <button type="button">{EXPORT_FORMAT_LABELS[format]}</button>
      {/*
        Empty on purpose. This is the live region a queued format's state is announced in — *Waiting
        for a worker…*, *Preparing your PDF… 3s*, *Download PDF · 84 KB* — and F6 fills it. It is
        rendered now, and only now, so that `getAllByRole('status')` in F5 resolves against a real
        element and the assertion about what it *says* is the thing that fails.
      */}
      {delivery === 'queued' && <p role="status" />}
    </div>
  );
}
