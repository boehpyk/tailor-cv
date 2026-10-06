export interface MoveAnnouncer {
  /** The polite live region's current text (`''` when there is nothing to say). */
  readonly message: string;
  /**
   * After a move: announce `message` and, once the card has re-rendered in its new column, move
   * focus onto that card's Move control (AC-33).
   */
  readonly announceMove: (cardId: string, message: string) => void;
  /** Announce without moving focus — *"Removed from your board…"* (AC-37). */
  readonly announce: (message: string) => void;
  /** A callback ref for one card's Move control, so focus can find it after the card moves. */
  readonly moveControlRef: (cardId: string) => (element: HTMLSelectElement | null) => void;
}

/**
 * The board's polite live region and focus-after-move (plan §7). Local state for the message and a
 * ref map of each card's Move control; the one honest `useEffect` focuses the moved card's control
 * after it re-renders in its new column — synchronizing with the DOM, which is what effects are for.
 *
 * SKELETON (T26): announces nothing, focuses nothing.
 */
export function useMoveAnnouncer(): MoveAnnouncer {
  return {
    message: '',
    announceMove: () => undefined,
    announce: () => undefined,
    moveControlRef: () => () => undefined,
  };
}
