import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ExportSheet } from './ExportSheet';

/**
 * web-design-refresh AC-23 — the export bar's mobile disclosure. RED-first (T24).
 *
 * Literal strings from the spec, never the copy constants. CSS visibility is not asserted: jsdom has
 * no layout, so R-2 (hidden at `sm`+, bottom sheet below) is a manual check.
 */
function renderSheet() {
  return render(
    <ExportSheet>
      <section aria-label="Download this document">
        <button type="button">Download PDF</button>
      </section>
    </ExportSheet>,
  );
}

const toggle = () => screen.getByRole('button', { name: 'Download' });

describe('ExportSheet', () => {
  it('starts closed', () => {
    renderSheet();
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens when the toggle is clicked', async () => {
    renderSheet();
    await userEvent.click(toggle());
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
  });

  it('aria-controls names the element that holds the children', () => {
    renderSheet();
    const id = toggle().getAttribute('aria-controls');
    expect(id).toBeTruthy();
    const sheet = document.getElementById(id ?? '');
    expect(sheet).not.toBeNull();
    expect(sheet).toContainElement(screen.getByRole('region', { name: 'Download this document' }));
  });

  it('Close closes the sheet and returns focus to the toggle', async () => {
    renderSheet();
    await userEvent.click(toggle());
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(toggle()).toHaveFocus();
  });

  it('Escape inside the sheet closes it and returns focus to the toggle', async () => {
    renderSheet();
    await userEvent.click(toggle());
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    screen.getByRole('button', { name: 'Download PDF' }).focus();
    await userEvent.keyboard('{Escape}');
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(toggle()).toHaveFocus();
  });

  it('renders the children exactly once, closed and open', async () => {
    renderSheet();
    expect(screen.getAllByRole('region', { name: 'Download this document' })).toHaveLength(1);
    await userEvent.click(toggle());
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('region', { name: 'Download this document' })).toHaveLength(1);
  });

  it('a remount is closed again and the open state touches no storage', async () => {
    localStorage.clear();
    sessionStorage.clear();
    const first = renderSheet();
    await userEvent.click(toggle());
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    first.unmount();
    renderSheet();
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });
});
