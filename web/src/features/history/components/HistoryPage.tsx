import { useHistory } from '../hooks/useHistory';

export interface HistoryPageProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every history key. */
  readonly userId: string;
}

/**
 * `/history` — a **container** (AC-44), under `RequireAuth` and `AccountScope`. Four states,
 * each deliberate: loading (`role="status"`), error (`role="alert"` + Retry — never the empty
 * state), empty (*Nothing tailored yet* + a link to the workspace), success (one
 * `HistoryEntryRow` per entry, and **Load more** — a button, not infinite scroll — iff there is a
 * next page, with its own pending state; a failed later page keeps the loaded rows, H-59). It owns
 * the delete mutation and the confirmation dialog (AC-45), and states how deletion works (AC-49).
 *
 * SKELETON (T29): the four states are distinguishable stubs; T31 builds them.
 */
export function HistoryPage({ userId }: HistoryPageProps): React.JSX.Element {
  const history = useHistory(userId);

  if (history.isError) {
    return <p role="alert">HistoryPage: error (skeleton)</p>;
  }
  if (history.isPending) {
    return <p role="status">HistoryPage: loading (skeleton)</p>;
  }
  const entries = history.data.pages.flatMap((page) => page.items);
  if (entries.length === 0) {
    return <p>HistoryPage: empty (skeleton)</p>;
  }
  return <p>HistoryPage: {entries.length} entries (skeleton)</p>;
}
