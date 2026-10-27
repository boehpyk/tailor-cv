/**
 * The PDF layout picker (slice 3.2, plan §7) — presentational: a native radio group over
 * `LAYOUT_TEMPLATES`. The selection is the caller's (`value`); the only state here is per card,
 * whether its preview failed to load.
 *
 * **Native radios, not a roving-tabindex widget.** One shared `name` gives Tab-into-the-group,
 * arrow keys to move and select, and Space to select, all from the browser (AC-34). The `<label>`
 * wraps the whole card, so the picture and the words are click targets too.
 *
 * Not gated by the save state: choosing a look writes nothing. Only the PDF control is (1.5's AC-40).
 */

import { useId, useState } from 'react';

import { PDF_LAYOUT_LEGEND, SELECTED_NOTE } from '../exportCopy';
import { LAYOUT_TEMPLATES } from '../layouts';

import type { LayoutTemplateOption } from '../layouts';
import type { LayoutTemplate } from '../types';

export interface LayoutPickerProps {
  /** The effective layout: the user's choice, else the derived pre-selection, else Classic. */
  readonly value: LayoutTemplate;
  readonly onChange: (layout: LayoutTemplate) => void;
  /** While the export list loads, so the selection cannot jump under the pointer (AC-32). */
  readonly disabled: boolean;
  /** Rendered as `aria-busy` on the fieldset. */
  readonly busy: boolean;
}

/** The previews' intrinsic size (`make layout.previews`), so a card reserves its space before load. */
const PREVIEW_WIDTH = 240;
const PREVIEW_HEIGHT = 340;

export function LayoutPicker({
  value,
  onChange,
  disabled,
  busy,
}: LayoutPickerProps): React.JSX.Element {
  const groupId = useId();
  return (
    <fieldset disabled={disabled} aria-busy={busy} className="min-w-0">
      <legend className="text-sm font-semibold text-slate-900">{PDF_LAYOUT_LEGEND}</legend>
      <div className="mt-3 flex flex-wrap gap-3">
        {LAYOUT_TEMPLATES.map((template) => (
          <LayoutCard
            key={template.id}
            template={template}
            idPrefix={`${groupId}-${template.id}`}
            checked={template.id === value}
            onSelect={onChange}
          />
        ))}
      </div>
    </fieldset>
  );
}

interface LayoutCardProps {
  readonly template: LayoutTemplateOption;
  readonly idPrefix: string;
  readonly checked: boolean;
  readonly onSelect: (layout: LayoutTemplate) => void;
}

function LayoutCard({ template, idPrefix, checked, onSelect }: LayoutCardProps): React.JSX.Element {
  // UI state, not server state: a preview that 404s or is blocked is hidden rather than shown as a
  // broken-image icon, and the name and description still say what the choice is (L-34).
  const [previewFailed, setPreviewFailed] = useState(false);
  const nameId = `${idPrefix}-name`;
  const descriptionId = `${idPrefix}-description`;

  return (
    <label className="flex min-w-0 flex-1 basis-24 cursor-pointer flex-col gap-2 rounded-xl border border-slate-200 bg-white p-2 text-sm has-checked:border-brand-600 has-checked:ring-1 has-checked:ring-brand-600 has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-600 has-disabled:cursor-not-allowed has-disabled:opacity-60 sm:max-w-56 sm:basis-40 sm:p-3">
      {!previewFailed && (
        // The paper on its desk: the preview sits in a grey well, as the editor's sheet does.
        <span className="flex justify-center rounded-lg bg-page-grey p-2 sm:p-3">
          <img
            src={template.previewUrl}
            alt=""
            width={PREVIEW_WIDTH}
            height={PREVIEW_HEIGHT}
            loading="lazy"
            decoding="async"
            onError={() => {
              setPreviewFailed(true);
            }}
            className="h-auto w-full max-w-28 rounded-sm bg-white shadow-sm"
          />
        </span>
      )}
      <span className="flex flex-wrap items-center gap-2">
        <input
          type="radio"
          name="pdf-layout"
          value={template.id}
          checked={checked}
          onChange={() => {
            onSelect(template.id);
          }}
          aria-labelledby={nameId}
          aria-describedby={descriptionId}
          className="accent-brand-600 focus-visible:outline-none"
        />
        <span id={nameId} className="font-display font-bold text-slate-900">
          {template.name}
        </span>
        {checked && (
          <span className="ml-auto rounded-full bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-700">
            {SELECTED_NOTE}
          </span>
        )}
      </span>
      <span id={descriptionId} className="text-xs text-slate-600">
        {template.description}
      </span>
    </label>
  );
}
