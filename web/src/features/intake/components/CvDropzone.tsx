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
      className={`rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors ${
        isDragOver ? 'border-slate-400 bg-slate-50' : 'border-slate-200'
      } ${disabled ? 'opacity-60' : ''}`}
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
      <label
        htmlFor="base-cv-file-input"
        className={`block text-sm font-medium ${
          disabled ? 'text-slate-400' : 'cursor-pointer text-slate-700 hover:text-slate-900'
        }`}
      >
        Drop your CV here — PDF, DOCX or TXT, up to 10 MB
      </label>

      {uploadingFileName !== null && (
        <div className="mt-3 space-y-1">
          <p className="text-sm text-slate-600">{uploadingFileName}</p>
          <p role="status" className="text-sm font-medium text-slate-500">
            Reading your CV…
          </p>
        </div>
      )}
    </div>
  );
}
