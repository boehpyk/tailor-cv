import { useBoard } from '../hooks/useBoard';

export interface BoardPageProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every tracking key. */
  readonly userId: string;
}

/**
 * `/board` — a **container** (AC-32), under `RequireAuth` and `AccountScope`.
 *
 * - **Loading:** `role="status"`, *"Loading your board…"*.
 * - **Error:** `role="alert"`, *"Couldn't load your board"* + **Retry** — never the empty state.
 * - **Empty:** *"Nothing on your board yet"* + a link to `/history` + the retention note.
 * - **Success:** six `BoardColumn`s in `STAGES` order, each a labelled region with its count;
 *   a `BoardCard` per card. The page owns the move and untrack mutations, `useCardDrag` and
 *   `useMoveAnnouncer` (the polite live region), and shows a refused move's copy in a
 *   `role="alert"` near the board (AC-34).
 *
 * SKELETON (T26): renders a bare marker per state — no copy, no roles; T28 builds it.
 */
export function BoardPage({ userId }: BoardPageProps): React.JSX.Element {
  const view = useBoard(userId);
  switch (view.status) {
    case 'loading':
      return <div data-skeleton="board-loading" />;
    case 'error':
      return <div data-skeleton="board-error" />;
    case 'ready':
      return view.cards.length === 0 ? (
        <div data-skeleton="board-empty" />
      ) : (
        <div data-skeleton="board-ready" />
      );
  }
}
