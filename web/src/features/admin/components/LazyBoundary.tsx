import { Component } from 'react';

import { LAZY_CHUNK_FAILED, LAZY_RELOAD_LABEL } from '../lazyCopy';

import type { ReactNode } from 'react';

export interface LazyBoundaryProps {
  readonly children: ReactNode;
}

interface LazyBoundaryState {
  readonly failed: boolean;
}

/**
 * The boundary around a lazy route (slice 4.1, AC-33, R-22). A rejected dynamic import — a tab
 * older than the last release asking for a chunk the new deploy no longer serves — throws out of
 * `lazy()` during render, and without a boundary that is a blank page.
 *
 * A **class**, because React 19 still has no hook for `getDerivedStateFromError`. Generic on
 * purpose: it knows nothing about admin, so 4.2's and later lazy routes wrap themselves in it too.
 *
 * **Reload, not retry.** `lazy()` caches its rejected promise, so re-rendering asks for the same
 * gone chunk; only a full `location.reload()` fetches the new `index.html` and its chunk names.
 *
 * It logs nothing: the error is a failed fetch of our own asset, and the boundary's view says so.
 */
export class LazyBoundary extends Component<LazyBoundaryProps, LazyBoundaryState> {
  override state: LazyBoundaryState = { failed: false };

  static getDerivedStateFromError(): LazyBoundaryState {
    return { failed: true };
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="mb-10 space-y-3">
        <p className="text-slate-700">{LAZY_CHUNK_FAILED}</p>
        <button
          type="button"
          onClick={() => {
            window.location.reload();
          }}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
        >
          {LAZY_RELOAD_LABEL}
        </button>
      </div>
    );
  }
}
