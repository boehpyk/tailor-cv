import { OFFLINE_SENTENCE, reconnectingSentence } from '../retryCopy';

import type { Connection } from '../hold';
import type { ConnectionSubject } from '../retryCopy';

export interface ConnectionNoteProps {
  readonly kind: ConnectionSubject;
  /** `connectionOf(query)`: what the poller is doing besides its normal tick. */
  readonly connection: Connection;
  /** The query's `failureCount`; the attempt shown is `failureCount + 1`. */
  readonly failureCount: number;
  /** The poller's `MAX_TRANSIENT_RETRIES`; the denominator shown is this `+ 1`. */
  readonly maxRetries: number;
}

/**
 * The `role="status"` line under a poller (AC-17, AC-18, AC-20): *reconnecting* while TanStack
 * retries a transient failure, *offline* while it has paused the fetch, and nothing for `'ok'`.
 *
 * A sub-state of "still working", worded so it can never be read as "this failed": the backoff it
 * describes is TanStack's own (plan §0.4), so the attempt shown is the attempt that runs. When the
 * retries are exhausted the query is in error and 1.3's *lost contact* takes over — one message at a
 * time (AC-19), which `connectionOf` guarantees by answering `'ok'` for `status === 'error'`.
 * Polite, never focused, never animated.
 */
export function ConnectionNote({
  kind,
  connection,
  failureCount,
  maxRetries,
}: ConnectionNoteProps): React.JSX.Element {
  // Always in the DOM (empty while `'ok'`), so a screen reader announces the change inside it.
  return (
    <p role="status" className="text-sm text-slate-600">
      {connection === 'ok'
        ? null
        : connection === 'paused'
          ? OFFLINE_SENTENCE[kind]
          : reconnectingSentence(kind, failureCount + 1, maxRetries + 1)}
    </p>
  );
}
