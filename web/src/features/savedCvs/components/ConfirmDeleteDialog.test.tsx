import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { confirmDeleteMessage } from '../savedCvsCopy';
import { ConfirmDeleteDialog } from './ConfirmDeleteDialog';

/**
 * T29 (`qa`, test-after) — AC-48: `ConfirmDeleteDialog`'s own a11y mechanism, in isolation from
 * `SavedBaseCvsSection` (which `SavedBaseCvsSection.test.tsx`'s AC-36 block already exercises
 * end-to-end, including the real "Delete" button as the opener). This file is what proves the
 * mechanism itself — labelling, initial focus, the trap in both Tab directions, and focus restored
 * on close — independent of any particular caller, using a plain button as a stand-in opener.
 */

function Harness({ onGone }: { readonly onGone: () => void }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
      >
        Open
      </button>
      {open && (
        <ConfirmDeleteDialog
          name="jane-cv.pdf"
          onConfirm={() => {
            setOpen(false);
            onGone();
          }}
          onCancel={() => {
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}

describe('ConfirmDeleteDialog — AC-36, AC-48', () => {
  it('is an alertdialog labelled by its heading and described by the confirmation message', () => {
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={vi.fn()} onCancel={vi.fn()} />);

    const dialog = screen.getByRole('alertdialog', { name: 'Delete saved CV' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription(confirmDeleteMessage('jane-cv.pdf'));
  });

  it('renders the name as a text node, never as markup, when it contains HTML-looking characters', () => {
    // AC-47: a label or filename is user text, not sanitized-and-trusted HTML.
    render(
      <ConfirmDeleteDialog
        name={'<script>alert(1)</script>.pdf'}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(
      screen.getByText(confirmDeleteMessage('<script>alert(1)</script>.pdf')),
    ).toBeInTheDocument();
    expect(document.querySelector('script')).not.toBeInTheDocument();
  });

  it('moves focus to Cancel on open — the safe choice for an irreversible action', () => {
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('traps Tab between the two controls in both directions, never leaving the dialog', async () => {
    const user = userEvent.setup();
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const cancelButton = screen.getByRole('button', { name: 'Cancel' });
    const confirmButton = screen.getByRole('button', { name: 'Delete' });
    expect(cancelButton).toHaveFocus();

    await user.tab();
    expect(confirmButton).toHaveFocus();

    await user.tab();
    expect(cancelButton).toHaveFocus();

    await user.tab({ shift: true });
    expect(confirmButton).toHaveFocus();
  });

  it('Escape calls onCancel', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={vi.fn()} onCancel={onCancel} />);

    await user.keyboard('{Escape}');

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Cancel calls onCancel and never onConfirm', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={onConfirm} onCancel={onCancel} />);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('Delete (confirm) calls onConfirm', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<ConfirmDeleteDialog name="jane-cv.pdf" onConfirm={onConfirm} onCancel={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Delete' }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('returns focus to whatever opened it once it unmounts', async () => {
    const user = userEvent.setup();
    let gone = false;
    render(<Harness onGone={() => (gone = true)} />);

    const opener = screen.getByRole('button', { name: 'Open' });
    opener.focus();
    await user.click(opener);
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(gone).toBe(false);
  });
});
