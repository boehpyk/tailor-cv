import type { SavedBaseCv } from '../types';
import type { ReactNode } from 'react';

export interface SavedBaseCvRowProps {
  readonly cv: SavedBaseCv;
  /**
   * A delete of this row is on the wire: the row reads "Deleting…" and its actions are disabled.
   * The row stays until the server answers — there is no optimistic removal (AC-36).
   */
  readonly isDeleting: boolean;
  /** The row's own notice — a delete that did not happen ("Not deleted — try again") — or `null`. */
  readonly notice: string | null;
  /** Rendered in place of the name while this row is being renamed; the section decides which row. */
  readonly renameForm: ReactNode;
  readonly onRename: () => void;
  readonly onDelete: () => void;
}

/**
 * One saved CV, presentational (AC-34): its display name (the label, else the filename), the
 * filename as well when it is labelled, its status — with the server's `failure_message` for
 * `extraction_failed` (AC-35) — its size, the date it was uploaded, and **Rename** and **Delete**.
 *
 * Every string that came from a user — the label, the filename — is a text node (AC-47).
 *
 * SKELETON (T25): renders nothing. GREEN is T27.
 */
// Typed as a function value, not declared with a parameter, only while it is a stub: the props are
// real and checked at every call site, and there is no unused parameter for the linter to refuse.
export const SavedBaseCvRow: (props: SavedBaseCvRowProps) => React.JSX.Element | null = () => null;
