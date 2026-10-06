import { useCallback, useEffect, useRef, useState } from 'react';

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
  readonly moveControlRef: (cardId: string) => React.RefCallback<HTMLSelectElement>;
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
 * The one honest `useEffect`: after every commit, if a focus is owed and the card's control exists
 * and is enabled, focus it and clear the debt. That is synchronizing with the DOM, which is what
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
    if (control !== undefined && !control.disabled) {
      control.focus();
      owedFocus.current = null;
    }
  });

  const announceMove = useCallback((cardId: string, text: string) => {
    owedFocus.current = cardId;
    setMessage(text);
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

  return { message, announceMove, announce, moveControlRef };
}
