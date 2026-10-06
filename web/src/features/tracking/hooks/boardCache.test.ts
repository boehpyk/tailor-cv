import { replaceEqualDeep } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';

import { makeCard, otherCard } from '../test/support';

import { shareCardsById } from './boardCache';

import type { Board, BoardCard } from '../types';

/**
 * AC-44 guard: `shareCardsById` keeps every unchanged card the very object it was, matched by id
 * rather than by position, so the memoized `BoardCard` skips it.
 *
 * Mutation-proven (3.1, test-after): replacing the body with TanStack's default
 * (`return replaceEqualDeep(previous, next)`) turns "keeps every unchanged card identical after a
 * re-sort" red (`expected { id: 'app-3', … } to be { id: 'app-3', … }`, Object.is, on the first card whose index
 * moved), and so does "a removed card" — the default compares arrays index by index and hands back
 * a fresh copy for each shifted card. The other cases hold under the default too (they pin the
 * guarantee a replacement must keep). Observed: 2 of 8 red.
 */

function board(...items: BoardCard[]): Board {
  return { items };
}

/** A deep copy, so a "refetch" shares no object with the cache — as a parsed response would not. */
function refetched(source: Board): Board {
  return structuredClone(source);
}

const A = otherCard(1);
const B = otherCard(2);
const C = otherCard(3);

describe('shareCardsById', () => {
  it('returns the incoming board on the first fetch, when there is no previous one', () => {
    const next = board(A, B);
    expect(shareCardsById(undefined, next)).toBe(next);
  });

  it('returns the previous board itself for an identical refetch', () => {
    const previous = board(A, B, C);
    expect(shareCardsById(previous, refetched(previous))).toBe(previous);
  });

  it('keeps every unchanged card identical when a refetch re-sorts them, and returns a new board', () => {
    const previous = board(A, B, C);
    const result = shareCardsById(previous, refetched(board(C, A, B))) as Board;

    expect(result).not.toBe(previous);
    expect(result.items.map((card) => card.id)).toEqual([C.id, A.id, B.id]);
    expect(result.items[0]).toBe(previous.items[2]);
    expect(result.items[1]).toBe(previous.items[0]);
    expect(result.items[2]).toBe(previous.items[1]);
  });

  it('gives a changed card a new object while the others keep their identity', () => {
    const previous = board(A, B, C);
    const changed = { ...refetched(board(B)).items[0], stage: 'offer' as const, version: 4 };
    const result = shareCardsById(previous, refetched(board(A, changed as BoardCard, C))) as Board;

    expect(result).not.toBe(previous);
    expect(result.items[1]).not.toBe(previous.items[1]);
    expect(result.items[1]).toMatchObject({ id: B.id, stage: 'offer', version: 4 });
    expect(result.items[0]).toBe(previous.items[0]);
    expect(result.items[2]).toBe(previous.items[2]);
  });

  it('produces a new board for an added card and keeps the existing cards', () => {
    const previous = board(A, B);
    const result = shareCardsById(previous, refetched(board(A, B, C))) as Board;

    expect(result).not.toBe(previous);
    expect(result.items).toHaveLength(3);
    expect(result.items[0]).toBe(previous.items[0]);
    expect(result.items[1]).toBe(previous.items[1]);
    expect(result.items[2]).toMatchObject({ id: C.id });
  });

  it('produces a new board for a removed card and keeps the survivors', () => {
    const previous = board(A, B, C);
    const result = shareCardsById(previous, refetched(board(A, C))) as Board;

    expect(result).not.toBe(previous);
    expect(result.items).toHaveLength(2);
    expect(result.items[0]).toBe(previous.items[0]);
    expect(result.items[1]).toBe(previous.items[2]);
  });

  it('produces a new board when one card is swapped for another of the same count', () => {
    const previous = board(A, B);
    const result = shareCardsById(previous, refetched(board(A, C))) as Board;

    expect(result).not.toBe(previous);
    expect(result.items[1]).toMatchObject({ id: C.id });
  });

  it('is a deliberate departure from the default, which re-copies shifted cards (the contrast)', () => {
    const previous = board(makeCard({ id: 'x' }), makeCard({ id: 'y' }));
    const next = refetched(board(previous.items[1] as BoardCard, previous.items[0] as BoardCard));
    const byDefault = replaceEqualDeep(previous, next);

    expect(byDefault.items[0]).not.toBe(previous.items[1]);
  });
});
