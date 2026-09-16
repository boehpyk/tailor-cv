import { act, renderHook } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { describe, expect, it } from 'vitest';

import { useWorkspaceTab } from './useWorkspaceTab';

import type { ReactNode } from 'react';

/**
 * F6 RED — the `?tab=` search parameter and its derived default (feature-spec AC-23,
 * technical-plan "Frontend" §`useWorkspaceTab`).
 *
 * Written against the **spec**, not `useWorkspaceTab.ts`'s F5 skeleton, which always returns
 * `{ tab: 'base-cv', setTab: () => undefined }` regardless of the URL or `hasBaseCv` — so every
 * case that expects `'job-posting'`, or expects `setTab` to change anything, fails on its assertion
 * (a wrong value read back), never on an `ImportError`: the hook exists, takes the real
 * `hasBaseCv: boolean` parameter, and returns the real `WorkspaceTabState` shape.
 *
 * A `MemoryRouter` wrapper is used even though the skeleton reads no router state today, because
 * the real implementation must (`?tab=` lives in the URL, not `useState` — technical-plan "The URL
 * as state"), and a test written against a bare render would need rewriting the moment F7 adds the
 * router hook it obviously needs.
 */

function wrapper(initialEntries: readonly string[]) {
  return function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <MemoryRouter initialEntries={[...initialEntries]}>{children}</MemoryRouter>;
  };
}

/** The hook under test, plus the URL's own search string — so a `setTab` call can be checked
 * against the URL React Router actually holds, not merely against a value the hook echoes back. */
function useWorkspaceTabAndLocation(hasBaseCv: boolean) {
  const state = useWorkspaceTab(hasBaseCv);
  const location = useLocation();
  return { ...state, search: location.search };
}

describe('useWorkspaceTab', () => {
  it('defaults to base-cv when there is no ?tab= and the session has no base CV', () => {
    const { result } = renderHook(() => useWorkspaceTabAndLocation(false), {
      wrapper: wrapper(['/']),
    });

    expect(result.current.tab).toBe('base-cv');
  });

  it('defaults to job-posting when there is no ?tab= and the session already has a base CV', () => {
    const { result } = renderHook(() => useWorkspaceTabAndLocation(true), {
      wrapper: wrapper(['/']),
    });

    expect(result.current.tab).toBe('job-posting');
  });

  it('reads ?tab=job-posting from the URL even when the session has no base CV', () => {
    const { result } = renderHook(() => useWorkspaceTabAndLocation(false), {
      wrapper: wrapper(['/?tab=job-posting']),
    });

    expect(result.current.tab).toBe('job-posting');
  });

  it('reads ?tab=base-cv from the URL even when the session already has a base CV', () => {
    const { result } = renderHook(() => useWorkspaceTabAndLocation(true), {
      wrapper: wrapper(['/?tab=base-cv']),
    });

    expect(result.current.tab).toBe('base-cv');
  });

  it('setTab writes the choice into the URL, and a re-render reads it back', () => {
    const { result } = renderHook(() => useWorkspaceTabAndLocation(false), {
      wrapper: wrapper(['/']),
    });
    expect(result.current.tab).toBe('base-cv');

    act(() => {
      result.current.setTab('job-posting');
    });

    expect(result.current.search).toContain('tab=job-posting');
    expect(result.current.tab).toBe('job-posting');
  });
});
