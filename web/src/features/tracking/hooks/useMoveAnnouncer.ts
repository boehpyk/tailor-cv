import { useCallback, useEffect, useRef, useState } from 'react';

export interface MoveAnnouncer {
  /** The polite live region's current text (`''` when there is nothing to say). */
  readonly message: string;
  /**
   * After a move: announce `message` and, once the card has re-rendered in its new column, move
   * focus onto that card's Move control (AC-33).
   */
  readonly announceMove: (cardId: string, message: string) => void;
  /**
   * After a refused move: once the card is back in its column and its control enabled, focus that
   * control — the one the user chose from, which the optimistic move had unmounted.
   */
  readonly returnFocus: (cardId: string) => void;
  /** Announce without moving focus — *"Removed from your board…"* (AC-37). */
  readonly announce: (message: string) => void;
  /** A callback ref for one card's Move control, so focus can find it after the card moves. */
  readonly moveControlRef: (cardId: string) => React.RefCallback<HTMLSelectElement>;
}

/** The attribute `BoardCard` puts on its root, so a control can find the card it belongs to. */
const BOARD_CARD_ATTRIBUTE = 'data-board-card';

/**
 * Whether focus is somewhere the user did not choose: on `<body>` (or nowhere), or still inside
 * the card whose control is owed it. Anywhere else, the user put it there.
 */
function focusIsLost(control: HTMLElement): boolean {
  const active = control.ownerDocument.activeElement;
  if (active === null || active === control.ownerDocument.body) {
    return true;
  }
  const card = control.closest(`[${BOARD_CARD_ATTRIBUTE}]`);
  return card !== null && card.contains(active);
}

/**
 * The board's polite live region and focus-after-move (plan §7).
 *
 * **Why focus needs help at all.** A card that changes column changes parent, so React unmounts it
 * from the old column and mounts a new one in the new column: the `<select>` that had focus is gone,
 * and focus falls to `<body>` — a keyboard user is thrown to the top of the page. The moved card's
 * new control has to be found and focused.
 *
 * **Why after the request settles, not at once.** While a move is on the wire its control is
 * `disabled` (T-39), and a disabled element cannot take focus. So the focus *target* is kept in a ref
 * until the control can take it.
 *
 * **Only if focus is still lost.** The debt is paid only when focus sits on `<body>` (where the
 * unmount dropped it) or inside the moved card itself. If the user has gone on to something else
 * while the move was on the wire — another card's title field, say — the debt is dropped: taking
 * focus from where a person put it is worse than not restoring it.
 *
 * The one honest `useEffect`: after every commit, if a focus is owed and the card's control exists
 * and is enabled, pay or drop the debt. That is synchronizing with the DOM, which is what
 * effects are for; it runs no state update, so it cannot loop. The control elements live in a ref
 * map filled by per-card callback refs, each cached so React does not detach and re-attach it on
 * every render, and each returning a React 19 ref cleanup.
 */
export function useMoveAnnouncer(): MoveAnnouncer {
  const [message, setMessage] = useState('');
  const controls = useRef(new Map<string, HTMLSelectElement>());
  const refCallbacks = useRef(new Map<string, React.RefCallback<HTMLSelectElement>>());
  const owedFocus = useRef<string | null>(null);

  useEffect(() => {
    const cardId = owedFocus.current;
    if (cardId === null) {
      return;
    }
    const control = controls.current.get(cardId);
    if (control === undefined || control.disabled) {
      return;
    }
    owedFocus.current = null;
    if (focusIsLost(control)) {
      control.focus();
    }
  });

  const announceMove = useCallback((cardId: string, text: string) => {
    owedFocus.current = cardId;
    setMessage(text);
  }, []);

  const returnFocus = useCallback((cardId: string) => {
    owedFocus.current = cardId;
  }, []);

  const announce = useCallback((text: string) => {
    setMessage(text);
  }, []);

  const moveControlRef = useCallback((cardId: string) => {
    const cached = refCallbacks.current.get(cardId);
    if (cached !== undefined) {
      return cached;
    }
    const callback: React.RefCallback<HTMLSelectElement> = (element) => {
      if (element === null) {
        return undefined;
      }
      controls.current.set(cardId, element);
      return () => {
        if (controls.current.get(cardId) === element) {
          controls.current.delete(cardId);
        }
      };
    };
    refCallbacks.current.set(cardId, callback);
    return callback;
  }, []);

  return { message, announceMove, returnFocus, announce, moveControlRef };
}
