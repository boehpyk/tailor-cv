import { renderHook } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useFragmentToken } from './fragmentToken';
import { TOKEN, openLink, resetAccountMailTest } from './test/support';

/**
 * T37 — `useFragmentToken` (AC-46, AC-48, AC-51). The hook was built for real at T35, so these may
 * **pass on arrival**; they are the contract written down from the spec, not a recorded red.
 *
 * The strip is observed on the real address bar (`window.location`) **and** through a
 * `history.replaceState` spy, whose call count is what "read once, stripped once" means under
 * `<StrictMode>`'s double mount.
 */

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('useFragmentToken', () => {
  it('returns the token from #token= and strips the fragment, keeping the path', () => {
    openLink('/confirm-email', TOKEN);
    const replaceState = vi.spyOn(window.history, 'replaceState');

    const { result } = renderHook(() => useFragmentToken());

    expect(result.current).toBe(TOKEN);
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(replaceState).toHaveBeenCalledWith(null, '', '/confirm-email');
    expect(window.location.hash).toBe('');
    expect(window.location.pathname).toBe('/confirm-email');
  });

  it('under <StrictMode> reads the same token and strips exactly once', () => {
    openLink('/reset-password/confirm', TOKEN);
    const replaceState = vi.spyOn(window.history, 'replaceState');

    const { result } = renderHook(() => useFragmentToken(), { wrapper: StrictMode });

    expect(result.current).toBe(TOKEN);
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(window.location.hash).toBe('');
  });

  it('with no fragment returns null and does not touch the history', () => {
    openLink('/confirm-email');
    const replaceState = vi.spyOn(window.history, 'replaceState');

    const { result } = renderHook(() => useFragmentToken());

    expect(result.current).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });

  it.each(['#', '#token=', '#other=abc', '#abc'])(
    'a fragment of %s carries no token: null',
    (hash) => {
      window.history.replaceState(null, '', `/confirm-email${hash}`);

      const { result } = renderHook(() => useFragmentToken());

      expect(result.current).toBeNull();
    },
  );

  it('a reload after the strip reads no fragment: the token lived in memory only', () => {
    openLink('/confirm-email', TOKEN);
    const first = renderHook(() => useFragmentToken());
    expect(first.result.current).toBe(TOKEN);
    first.unmount();

    const second = renderHook(() => useFragmentToken());

    expect(second.result.current).toBeNull();
  });

  it('keeps the token across re-renders after the address bar has been stripped', () => {
    openLink('/confirm-email', TOKEN);

    const { result, rerender } = renderHook(() => useFragmentToken());
    rerender();
    rerender();

    expect(window.location.hash).toBe('');
    expect(result.current).toBe(TOKEN);
  });
});
