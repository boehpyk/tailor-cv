/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the props are the signature qa's T27 tests compile against; T28 uses them and deletes this line. */
export interface CardTitleEditorProps {
  /** The signed-in user — roots the retitle mutation's key. */
  readonly userId: string;
  /** The card being retitled. */
  readonly applicationId: string;
  /** The card's own title, `null` when it has none (the input then starts empty). */
  readonly title: string | null;
  /** The `version` the board shows — sent with the `PUT`. */
  readonly version: number;
}

/**
 * **Edit title** → an inline, labelled text input (AC-36) — a **container** over
 * `useRetitleTrackedApplication`. The text being typed is local `useState` (form state); the
 * counter is visible (*"n / 120"*); Enter or **Save** sends the title; **Clear title** sends `null`;
 * Escape cancels and returns focus to **Edit title**. 422 → the boundary's message under the field,
 * linked by `aria-describedby`, the input kept; 409 → as a move's (AC-34). Not optimistic.
 *
 * SKELETON (T26): renders nothing; T28 builds it.
 */
export function CardTitleEditor(_props: CardTitleEditorProps): React.JSX.Element | null {
  return null;
}
