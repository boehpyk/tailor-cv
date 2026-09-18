/**
 * The export bar — **SKELETON (F4). It is deliberately non-functional.**
 *
 * What ships here is four `<button>`s in AC-36's order, carrying AC-36's labels, and a
 * `role="status"` for each of the two queued formats. That is the whole component. There is **no**
 * `useExportJobs`, no `useRequestExport`, no `useDownload`, no `viewOfExport`, no elapsed tick, no
 * save-state gate, no AC-35 sentence and no loading / error / empty branch. Every one of those is
 * **F6**.
 *
 * ## Why this file does nothing
 *
 * The red-first tiers (docs/sdlc.md §2) make the loading / error / empty / success contract a test
 * written *before* its implementation — and a test is only worth the failure you watched it
 * produce. A red that says "unable to find an element with the role status" proves the file is
 * missing; it says nothing about whether the assertion discriminates the state it claims to guard.
 *
 * Four slices have now paid for that distinction the expensive way. **1.1's T33/T34, 1.3's T40,
 * 1.4's F5 and F8** each shipped a frontend skeleton that already worked, so `qa`'s tests passed on
 * arrival: they were never observed failing, and a test nobody has seen fail is a claim nobody has
 * checked. F5 — the table over every view state, the polling-stops assertion, the save gate, the
 * blob path — is the most valuable test on this frontend, and it is worth nothing if this bar
 * already renders what it asserts.
 *
 * So: real roles, real accessible names, real props. Absent behaviour. F5 reds on every row that
 * matters, and F6 turns it green.
 */

import { ExportControl } from './ExportControl';

import type { ExportControlProps } from './ExportControl';
import type { SaveState } from '@/features/editor/saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface ExportBarProps {
  /**
   * The run whose exports these are — **the id, not the run**.
   *
   * The plan writes `<ExportBar run={…} />` and its own state table then says the bar reads the run
   * "for nothing but the id (`current` comes from the server)". Taking the id makes that true by
   * construction. The one judgement a `TailoringRun` could contribute here is
   * `job.run_version === run.version`, which is the cross-aggregate comparison the API already made
   * and shipped as `ExportJob.current` — a `run` prop would be a standing invitation to re-derive
   * it in TypeScript (Constitution §4.5). `viewOfExport` dropped the same parameter at F3, for the
   * same reason.
   */
  readonly runId: string;
  /**
   * Which document the four controls act on: the visible editor tab, which is the URL's
   * `:document` segment (1.4's rule — the visible tab is the URL, never a `useState`).
   *
   * Named as the wire and `ExportTarget` name it. Switching the tab switches which document's jobs
   * the controls reflect (AC-36): a PDF ready for the CV is not shown as ready for the letter.
   */
  readonly document: TailoredDocumentKind;
  /**
   * The **visible document's** autosave state, owned by 1.4's machine and passed down (F7 wires it
   * through `DocumentWorkspace`). The bar reads it and never holds it.
   *
   * It is the AC-40 gate: exports are offered only while the state is `saved`, because the server
   * renders the text *it* holds, and exporting a document the user is still typing into produces a
   * file that silently does not match the screen. F6 implements the gate; here the prop exists only
   * so that F5 can render this component with the signature F6 will honour.
   */
  readonly saveState: SaveState;
}

/**
 * The four controls, in AC-36's order, each with the delivery that decides its roles.
 *
 * A table rather than four hand-written elements: the order is specified (*Markdown*, *Plain text*,
 * *PDF*, *Word* — the two free, instant formats first, then the two that cost a worker), and a
 * table keeps the order and the queued/inline split in one readable place instead of spread across
 * four call sites. Typed as `ExportControlProps` so the two lists cannot drift apart.
 */
const EXPORT_CONTROLS: readonly ExportControlProps[] = [
  { format: 'md', delivery: 'inline' },
  { format: 'txt', delivery: 'inline' },
  { format: 'pdf', delivery: 'queued' },
  { format: 'docx', delivery: 'queued' },
];

export function ExportBar(props: ExportBarProps): React.JSX.Element {
  // Declared and deliberately unread — `noUnusedParameters` is on, and renaming these to `_props`
  // would hide the signature from whoever reads this next. Discarding the value explicitly says
  // "the contract is fixed, the behaviour is F6" in a way that cannot be mistaken for an oversight.
  // F6 deletes this line by using all three fields. (`exportView.ts`'s stub made the same call,
  // including this disable: the rule is right in general — `void` on something that is not a call
  // discards nothing — and this is the one situation where discarding nothing is exactly the
  // intent. Disabled by name, for one line, rather than weakened in `eslint.config.js`.)
  // eslint-disable-next-line @typescript-eslint/no-meaningless-void-operator
  void props;

  return (
    <div>
      {EXPORT_CONTROLS.map((control) => (
        <ExportControl key={control.format} {...control} />
      ))}
    </div>
  );
}
