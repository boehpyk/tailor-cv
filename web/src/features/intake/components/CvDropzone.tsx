import { useState } from 'react';

interface CvDropzoneProps {
  /** Disables the control — true while a chosen file is uploading (bounds F-21). */
  readonly disabled: boolean;
  /** The filename of the file currently uploading, or `null` when nothing is in flight. */
  readonly uploadingFileName: string | null;
  readonly onFileChosen: (file: File) => void;
}

/**
 * The upload control — presentational, drag-and-drop **and** a real `<input type="file">`.
 *
 * The labelled input is the primary control, not a fallback: a div that only responds to
 * `drop` is invisible to a keyboard or a screen reader (technical-plan.md's Components list). The
 * drag-and-drop handlers add a hover affordance on top of it; they never do anything the label
 * click / native file picker cannot also do. The drag-hover flag is local `useState` — it is
 * purely a transient rendering detail of this one component, not state anything else needs.
 */
export function CvDropzone({
  disabled,
  uploadingFileName,
  onFileChosen,
}: CvDropzoneProps): React.JSX.Element {
  const [isDragOver, setIsDragOver] = useState(false);

  function handleDragOver(event: React.DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    if (!disabled) {
      setIsDragOver(true);
    }
  }

  function handleDragLeave(): void {
    setIsDragOver(false);
  }

  function handleDrop(event: React.DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    setIsDragOver(false);
    if (disabled) {
      return;
    }
    const file = event.dataTransfer.files[0];
    if (file !== undefined) {
      onFileChosen(file);
    }
  }

  function handleInputChange(event: React.ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    if (file !== undefined) {
      onFileChosen(file);
    }
    // Reset the input so choosing the same filename again (e.g. after fixing and re-saving a file
    // that failed) fires a change event instead of being silently ignored.
    event.target.value = '';
  }

  return (
    <div
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      // The drag-over state is an attribute, so the style and the test read the same fact
      // (web-design-refresh AC-15); absent rather than "false" when not dragging.
      data-drag-over={isDragOver ? 'true' : undefined}
      className={`flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-slate-300 bg-white px-6 py-12 text-center transition-colors has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-600 data-[drag-over=true]:border-brand-600 data-[drag-over=true]:bg-brand-50 max-sm:py-6 ${
        disabled ? 'opacity-60' : ''
      }`}
    >
      {/* Visually hidden, never `hidden`/`display:none` — it must stay in the tab order so a
          keyboard user reaches it. The label below is the visible surface; clicking anywhere on it
          opens the native file picker exactly as clicking the input itself would. */}
      <input
        id="base-cv-file-input"
        type="file"
        accept=".pdf,.docx,.txt"
        disabled={disabled}
        onChange={handleInputChange}
        className="sr-only"
      />
      <span
        aria-hidden="true"
        className="flex size-14 items-center justify-center rounded-xl border border-slate-200 bg-white text-brand-600 max-sm:hidden"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className="size-6"
        >
          <path d="M12 16V4m0 0-5 5m5-5 5 5M5 20h14" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      {/* OQ-18: a visual split only. The label's text — the input's accessible name — is still
          exactly "Drop your CV here — PDF, DOCX or TXT, up to 10 MB": the dash is visually hidden,
          not removed, so the two lines read as one sentence to a screen reader. */}
      <label
        htmlFor="base-cv-file-input"
        className={`block text-slate-700 ${disabled ? '' : 'cursor-pointer hover:text-slate-900'}`}
      >
        <span className="block font-display text-xl font-bold text-slate-900">
          Drop your CV here
        </span>
        <span className="sr-only"> — </span>
        <span className="mt-1 block text-sm">PDF, DOCX or TXT, up to 10 MB</span>
      </label>

      {uploadingFileName !== null && (
        <div className="space-y-1">
          <p className="text-sm font-medium text-slate-900">{uploadingFileName}</p>
          <p role="status" className="text-sm text-slate-600">
            Reading your CV…
          </p>
        </div>
      )}
    </div>
  );
}
