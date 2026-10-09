import type { Connection } from '../hold';
import type { ConnectionSubject } from '../retryCopy';

/**
 * The `role="status"` line under a poller (AC-17, AC-18, AC-20): *reconnecting* while TanStack
 * retries, *offline* while it is paused; nothing for `'ok'`.
 *
 * T7 SKELETON: renders nothing.
 */
export interface ConnectionNoteProps {
  readonly kind: ConnectionSubject;
  readonly connection: Connection;
  /** The query's `failureCount`; the attempt shown is `failureCount + 1`. */
  readonly failureCount: number;
  /** The poller's `MAX_TRANSIENT_RETRIES`; the denominator shown is this `+ 1`. */
  readonly maxRetries: number;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function ConnectionNote(_props: ConnectionNoteProps): React.JSX.Element | null {
  return null;
}
