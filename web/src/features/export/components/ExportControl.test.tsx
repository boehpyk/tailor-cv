import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ExportControl } from './ExportControl';

import type { ExportView } from '../exportView';
import type { ExportFormat } from '../types';

/**
 * F8 — `ExportControl`'s structure and markup, in isolation from the network and the bar.
 *
 * This tier is **test-after by design** (docs/sdlc.md §2): what is asserted below — DOM order, one
 * live region per control, how a byte size prints on a button, where a failure's message sits
 * relative to its retry control — is discovered against React Testing Library and JSX, not designed
 * ahead of the component the way `viewOfExport`'s table (`exportView.test.ts`, F5) or the bar's
 * behaviour (`ExportBar.test.tsx`, F5/F6) were.
 *
 * `ExportBar.test.tsx` already drives every one of these views through the real network stub, the
 * two mutations and a `QueryClient`; this file skips all of that and constructs the `ExportView` by
 * hand, because `ExportControl` is presentational (its own docstring: "it calls no hook, holds no
 * state") and the question here is what it renders for a given view, not how that view arose.
 *
 * Every literal string below is copied from `feature-spec.md` (AC-36, AC-37, AC-42) verbatim, never
 * imported from `exportCopy.ts` — importing the constant under test would make an assertion agree
 * with a copy module that could itself be wrong, and never notice.
 */

function renderControl(overrides: {
  readonly format?: ExportFormat;
  readonly view: ExportView;
  readonly disabled?: boolean;
  readonly secondsOnPage?: number;
  readonly onPrimary?: () => void;
}): { readonly container: HTMLElement } {
  const {
    format = 'pdf',
    view,
    disabled = false,
    secondsOnPage = 0,
    onPrimary = vi.fn(),
  } = overrides;
  const { container } = render(
    <ExportControl
      format={format}
      view={view}
      disabled={disabled}
      secondsOnPage={secondsOnPage}
      onPrimary={onPrimary}
    />,
  );
  return { container };
}

describe('ExportControl structure', () => {
  it('renders exactly one button followed by exactly one role="status" region, in that order', () => {
    const { container } = renderControl({ view: { kind: 'idle' } });

    const root = container.firstElementChild;
    expect(root).not.toBeNull();
    const children = root === null ? [] : Array.from(root.children);

    expect(children).toHaveLength(2);
    expect(children[0]?.tagName).toBe('BUTTON');
    expect(children[1]?.getAttribute('role')).toBe('status');
    expect(within(container).getAllByRole('status')).toHaveLength(1);
  });

  it('AC-40: reflects the disabled prop on the one button it renders', () => {
    renderControl({ view: { kind: 'idle' }, disabled: true });

    expect(screen.getByRole('button')).toBeDisabled();
  });
});

describe('AC-37: byte-size formatting on the ready label', () => {
  it('a known byte size renders "Download PDF · 84 KB"', () => {
    renderControl({
      format: 'pdf',
      view: { kind: 'ready', jobId: 'job-1', byteSize: 86016 },
    });

    expect(screen.getByRole('button', { name: 'Download PDF · 84 KB' })).toBeInTheDocument();
  });

  it('an unknown byte size (null) renders "Download PDF" with no size clause', () => {
    renderControl({
      format: 'pdf',
      view: { kind: 'ready', jobId: 'job-1', byteSize: null },
    });

    // Exact match: a control that appended "· null KB" or "· undefined" would still contain
    // "Download PDF" as a substring, which is exactly why this is not `{ exact: false }`.
    expect(screen.getByRole('button', { name: 'Download PDF' })).toBeInTheDocument();
  });
});

describe('AC-42: the not-found and expired download-failure copies', () => {
  it('"We couldn\'t find that file" sits in the status region, ahead of Try again', () => {
    renderControl({
      format: 'pdf',
      view: {
        kind: 'downloadFailed',
        message: "We couldn't find that file",
        nextAction: 'request',
      },
    });

    const status = screen.getByRole('status');
    expect(within(status).getByText("We couldn't find that file")).toBeInTheDocument();
    const retry = within(status).getByRole('button', { name: 'Try again' });
    expect(retry).toBeInTheDocument();

    // Placement, not merely presence: the message precedes the control that acts on it, which is
    // what lets a screen-reader user hear what went wrong before being told there is something to
    // click about it.
    const text = status.textContent;
    expect(text.indexOf("We couldn't find that file")).toBeLessThan(text.indexOf('Try again'));
  });

  it('"Your session has expired" sits in the status region, ahead of Try again', () => {
    renderControl({
      format: 'pdf',
      view: { kind: 'downloadFailed', message: 'Your session has expired', nextAction: 'download' },
    });

    const status = screen.getByRole('status');
    expect(within(status).getByText('Your session has expired')).toBeInTheDocument();
    expect(within(status).getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    const text = status.textContent;
    expect(text.indexOf('Your session has expired')).toBeLessThan(text.indexOf('Try again'));
  });

  it('clicking Try again on a downloadFailed view calls onPrimary', () => {
    const onPrimary = vi.fn();
    renderControl({
      format: 'pdf',
      view: {
        kind: 'downloadFailed',
        message: "We couldn't find that file",
        nextAction: 'request',
      },
      onPrimary,
    });

    screen.getByRole('button', { name: 'Try again' }).click();

    expect(onPrimary).toHaveBeenCalledTimes(1);
  });
});
