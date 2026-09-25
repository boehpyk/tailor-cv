import type { SavedBaseCv } from '../types';

export interface RenameLabelFormProps {
  /** The CV being renamed; its current label (or none) seeds the input. */
  readonly cv: SavedBaseCv;
  /** The form is finished — saved, or cancelled. The section stops rendering it. */
  readonly onDone: () => void;
}

/**
 * The inline rename form (AC-37). The draft is **local form state** (`useState`), seeded from the
 * CV once and never synced back from the query. Owns its own `useRenameSavedBaseCv` so its pending
 * and error are its own: a labelled input, Save / Cancel, "Saving…" while pending, and a refusal
 * (`invalid_label` and the rest) as `role="alert"` linked to the input by `aria-describedby`.
 *
 * SKELETON (T25): renders nothing. GREEN is T27.
 */
// Typed as a function value, not declared with a parameter, only while it is a stub: the props are
// real and checked at every call site, and there is no unused parameter for the linter to refuse.
export const RenameLabelForm: (props: RenameLabelFormProps) => React.JSX.Element | null = () =>
  null;
