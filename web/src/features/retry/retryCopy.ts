/** The shared sentences of slice 3.3 (feature-spec F-9, F-12, F-14; AC-17, AC-18, AC-20). */

/** The `role="status"` line when a hold ends (AC-9…AC-15, AC-23). */
export const TRY_AGAIN_NOW = 'You can try again now.';

/** Which poller a connection line speaks for. */
export type ConnectionSubject = 'run' | 'export';

const STILL_WORKING: Readonly<Record<ConnectionSubject, string>> = {
  run: 'Your run is still working.',
  export: 'Your file is still being prepared.',
};

/** AC-18 / AC-20: TanStack paused the poll because the browser is offline. */
export const OFFLINE_SENTENCE: Readonly<Record<ConnectionSubject, string>> = {
  run: "You're offline. Your run keeps going on our side — we'll check again when you're back.",
  export:
    "You're offline. Your file is still being prepared on our side — we'll check again when you're back.",
};

/** AC-17 / AC-20: *"Connection trouble — trying again (attempt 2 of 4). Your run is still working."* */
export function reconnectingSentence(
  subject: ConnectionSubject,
  attempt: number,
  maxAttempts: number,
): string {
  return `Connection trouble — trying again (attempt ${String(attempt)} of ${String(maxAttempts)}). ${STILL_WORKING[subject]}`;
}
