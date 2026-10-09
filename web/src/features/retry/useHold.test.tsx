import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { useErrorHold, useHold } from './useHold';

// 2023-11-14T22:13:20Z. Fake timers fake `Date` too, so "now" is exactly this until advanced.
const NOW = 1_700_000_000_000;

function tooMany(seconds: number): ApiError {
  return new ApiError(429, 'Too many.', 'rate_limited', {}, seconds);
}

// Props typed `unknown`, so a rerender may pass `null` or another error.
const widen = (e: unknown): unknown => e;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe('useHold (AC-7)', () => {
  it('is held at deadline - 1 ms and released at exactly the deadline', () => {
    // Mutation note (1): `<=` for `<` in `held` keeps the hold one instant too long, and the
    // second assertion (released at the deadline) goes red.
    const { result } = renderHook(() => useHold(NOW + 5000));
    expect(result.current.held).toBe(true); // positive control: it does start held

    act(() => {
      vi.advanceTimersByTime(4999);
    });
    expect(result.current.held).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(Date.now()).toBe(NOW + 5000);
    expect(result.current.held).toBe(false);
    expect(result.current.remainingSeconds).toBe(0);
  });

  it('re-renders once per second with the whole seconds left', () => {
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useHold(NOW + 3000);
    });
    expect(result.current.remainingSeconds).toBe(3);
    const before = renders;

    for (const expected of [2, 1]) {
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(result.current.remainingSeconds).toBe(expected);
    }
    expect(renders).toBeGreaterThanOrEqual(before + 2);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.remainingSeconds).toBe(0);
    expect(result.current.held).toBe(false);
  });

  it('a new deadline restarts the hold', () => {
    const { result, rerender } = renderHook(({ d }) => useHold(d), {
      initialProps: { d: NOW + 2000 },
    });
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.held).toBe(false); // released: the control for what follows

    rerender({ d: Date.now() + 5000 });
    expect(result.current.held).toBe(true);
    act(() => {
      vi.advanceTimersByTime(4999);
    });
    expect(result.current.held).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.held).toBe(false);
  });

  it('a deadline already past is not held', () => {
    const { result } = renderHook(() => useHold(NOW - 1));
    expect(result.current.held).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('null holds nothing and arms no timer', () => {
    // Positive control: a real deadline does arm a timer, so a count of 0 below is a statement.
    const armed = renderHook(() => useHold(NOW + 5000));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    armed.unmount();

    const { result } = renderHook(() => useHold(null));
    expect(result.current.held).toBe(false);
    expect(result.current.remainingSeconds).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('unmounting clears its timers', () => {
    const { unmount } = renderHook(() => useHold(NOW + 5000));
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  it('derives remaining from Date.now() on render, so a throttled tab is right on return', () => {
    const { result, rerender } = renderHook(() => useHold(NOW + 60_000));
    expect(result.current.remainingSeconds).toBe(60);

    vi.setSystemTime(NOW + 45_000); // the tab slept: no tick ran
    rerender();

    expect(result.current.remainingSeconds).toBe(15);
  });
});

describe('useErrorHold (AC-7)', () => {
  it('holds for the 429 window from the moment the error arrived, and reports the deadline', () => {
    const error = tooMany(30);
    const { result } = renderHook(() => useErrorHold(error));

    expect(result.current.held).toBe(true);
    expect(result.current.deadlineMs).toBe(NOW + 30_000);

    act(() => {
      vi.advanceTimersByTime(29_999);
    });
    expect(result.current.held).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.held).toBe(false);
  });

  it('is stamped at receipt: re-rendering the same error later does not extend the hold', () => {
    const error = tooMany(30);
    const { result, rerender } = renderHook(() => useErrorHold(error));
    vi.setSystemTime(NOW + 20_000);

    rerender();

    expect(result.current.deadlineMs).toBe(NOW + 30_000);
    expect(result.current.remainingSeconds).toBe(10);
  });

  it('a new error object restarts the hold from its own arrival', () => {
    const { result, rerender } = renderHook(({ e }) => useErrorHold(e), {
      initialProps: { e: widen(tooMany(30)) },
    });
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(result.current.held).toBe(false); // released: the control

    rerender({ e: tooMany(10) });

    expect(result.current.held).toBe(true);
    expect(result.current.deadlineMs).toBe(NOW + 40_000);
  });

  it('releases when the error goes back to null (mutation.reset()) and leaves no timer', () => {
    const { result, rerender } = renderHook(({ e }) => useErrorHold(e), {
      initialProps: { e: widen(tooMany(30)) },
    });
    expect(result.current.held).toBe(true);
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    rerender({ e: null });

    expect(result.current.held).toBe(false);
    expect(result.current.deadlineMs).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('an error that is not a 429 never holds', () => {
    const control = renderHook(() => useErrorHold(tooMany(30)));
    expect(control.result.current.held).toBe(true);
    control.unmount();

    const { result } = renderHook(() => useErrorHold(new ApiError(503, 'Down.', null, {}, 30)));

    expect(result.current.held).toBe(false);
    expect(result.current.deadlineMs).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
});
