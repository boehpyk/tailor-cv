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
 * The button labels are the contract the tests click by. Skeleton (F8): the buttons, wired to
 * the props; the sentence above them is F10c's.
 */
export function ConflictNotice({
  onLoadLatest,
  onKeepMine,
}: ConflictNoticeProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap gap-2">
      <button
        type="button"
        onClick={onLoadLatest}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800"
      >
        Load the latest version
      </button>
      <button
        type="button"
        onClick={onKeepMine}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800"
      >
        Keep my version
      </button>
    </div>
  );
}
