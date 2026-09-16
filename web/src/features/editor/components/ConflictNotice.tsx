export interface ConflictNoticeProps {
  /** Re-seed the editor from the server's text, discarding the local edits (AC-33). */
  readonly onLoadLatest: () => void;
  /** Send the local text once more, against the fresh version (AC-33). */
  readonly onKeepMine: () => void;
}

/**
 * The two explicit choices after a 409 whose server text differs from ours (E-8, E-17) —
 * presentational. **Neither happens without a click** (AC-33): this component offers the choice
 * and does nothing on its own, which is the whole reason it is a component and not a `confirm()`.
 *
 * The button labels are the contract the tests click by. The sentence names what each choice
 * costs, because one of them discards work: *Load the latest version* throws away what was typed
 * here, *Keep my version* overwrites what was saved elsewhere, and a person choosing between the
 * two deserves to know which is which before the click.
 */
export function ConflictNotice({
  onLoadLatest,
  onKeepMine,
}: ConflictNoticeProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <p className="text-sm text-slate-700">
        This document was changed elsewhere — in another tab, or another window. Load the latest
        version to replace what is here, or keep your version to overwrite it.
      </p>
      <button
        type="button"
        onClick={onLoadLatest}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50"
      >
        Load the latest version
      </button>
      <button
        type="button"
        onClick={onKeepMine}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50"
      >
        Keep my version
      </button>
    </div>
  );
}
