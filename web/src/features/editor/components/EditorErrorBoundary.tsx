import { Component } from 'react';

import type { ReactNode } from 'react';

export interface EditorErrorBoundaryProps {
  /** What to show instead of the editor — 1.3's read-only preview (AC-35). */
  readonly fallback: ReactNode;
  readonly children: ReactNode;
}

interface EditorErrorBoundaryState {
  readonly failed: boolean;
}

/**
 * The boundary around the editor (AC-35, E-29). A class, because React has no hook for
 * `getDerivedStateFromError` — this is the one place in `web/` a class component is the idiom
 * rather than a habit.
 *
 * **What it logs, and what it never logs.** `componentDidCatch` writes the error's **type** to
 * `console.error` and nothing else: a ProseMirror error quotes the node it rejected, and a
 * `RangeError: Invalid content for node paragraph: <text("…")>` is a line of somebody's CV in the
 * console. The message is not ours to print. (React's own development-mode report of a caught
 * error is React's; the production build's `onCaughtError` is where that report is silenced, and
 * it is F11's wiring, not this component's.)
 *
 * **No reset.** A document the bridge cannot parse will not parse on the next render either; the
 * fallback stays until the run changes, and the parent keys on the run.
 */
export class EditorErrorBoundary extends Component<
  EditorErrorBoundaryProps,
  EditorErrorBoundaryState
> {
  override state: EditorErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): EditorErrorBoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: unknown): void {
    const errorType = error instanceof Error ? error.name : typeof error;
    console.error('editor: the document could not be opened in the editor', { errorType });
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
