import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CvDropzone } from './CvDropzone';

/** The label text on the control's `<input type="file">` (technical-plan.md's empty-state row). */
const DROPZONE_LABEL = /drop your cv here/i;

/** The dragover/dragleave/drop handlers live on the input's parent `<div>` (CvDropzone.tsx). Found
 * by DOM traversal from the accessible input, not by a class selector. */
function dropzoneOf(input: HTMLElement): HTMLElement {
  const dropzone = input.parentElement;
  if (dropzone === null) {
    throw new Error('the file input has no parent element to drag/drop onto');
  }
  return dropzone;
}

describe('CvDropzone', () => {
  it('exposes a real, keyboard-reachable file input with an accessible name and the accepted formats', () => {
    render(<CvDropzone disabled={false} uploadingFileName={null} onFileChosen={vi.fn()} />);

    const input = screen.getByLabelText<HTMLInputElement>(DROPZONE_LABEL);

    // A div-only dropzone is the classic inaccessible upload control (CvDropzone.tsx's own
    // docblock). `toBeVisible` fails on the `hidden` attribute or an inline `display:none` /
    // `visibility:hidden` — exactly what "tidying the input into hidden" would introduce — while
    // still allowing the `sr-only` class the component actually uses to keep it visually collapsed.
    expect(input).toBeVisible();
    expect(input.type).toBe('file');
    expect(input).not.toHaveAttribute('tabindex', '-1');
    expect(input).toHaveAttribute('accept', '.pdf,.docx,.txt');
  });

  it('disables the input and shows the chosen filename and status while uploading', () => {
    render(<CvDropzone disabled={true} uploadingFileName="resume.pdf" onFileChosen={vi.fn()} />);

    expect(screen.getByLabelText(DROPZONE_LABEL)).toBeDisabled();
    expect(screen.getByText('resume.pdf')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Reading your CV…');
  });

  it('shows the hover affordance while a file is dragged over it, and clears it on dragleave', () => {
    render(<CvDropzone disabled={false} uploadingFileName={null} onFileChosen={vi.fn()} />);
    const dropzone = dropzoneOf(screen.getByLabelText(DROPZONE_LABEL));

    expect(dropzone).not.toHaveClass('bg-slate-50');

    fireEvent.dragOver(dropzone);
    expect(dropzone).toHaveClass('bg-slate-50');

    fireEvent.dragLeave(dropzone);
    expect(dropzone).not.toHaveClass('bg-slate-50');
  });

  it('does not show the hover affordance while disabled', () => {
    render(<CvDropzone disabled={true} uploadingFileName={null} onFileChosen={vi.fn()} />);
    const dropzone = dropzoneOf(screen.getByLabelText(DROPZONE_LABEL));

    fireEvent.dragOver(dropzone);

    expect(dropzone).not.toHaveClass('bg-slate-50');
  });

  it('hands the dropped file to onFileChosen', () => {
    const onFileChosen = vi.fn();
    render(<CvDropzone disabled={false} uploadingFileName={null} onFileChosen={onFileChosen} />);
    const dropzone = dropzoneOf(screen.getByLabelText(DROPZONE_LABEL));
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });

    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });

    expect(onFileChosen).toHaveBeenCalledWith(file);
  });

  it('does not hand a dropped file to onFileChosen while disabled', () => {
    const onFileChosen = vi.fn();
    render(<CvDropzone disabled={true} uploadingFileName={null} onFileChosen={onFileChosen} />);
    const dropzone = dropzoneOf(screen.getByLabelText(DROPZONE_LABEL));
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });

    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });

    expect(onFileChosen).not.toHaveBeenCalled();
  });
});
