/**
 * The PDF layouts the picker offers (slice 3.2, plan §0.9) — a client mirror, not a fetched
 * catalogue: the bundle must ship one preview per layout, so it already knows every id, and an
 * endpoint would add a request plus loading and error states that exist only because of it.
 *
 * **Pinned to `LayoutTemplate` in `api/src/tailorcraft/domain/export/value_objects.py`**: same ids,
 * same order (AC-1 on that side, AC-29 on this one). Never mirrored here: the default layout (the
 * client always sends an id for a PDF) or anything else that is a business rule.
 *
 * The previews are Vite-imported, so each is a hashed asset URL, rendered by `make
 * layout.previews` from a synthetic CV through the real PDF pipeline.
 */

import classicPreview from './assets/layout-classic.webp';
import formalPreview from './assets/layout-formal.webp';
import modernPreview from './assets/layout-modern.webp';
import type { LayoutTemplate } from './types';

export interface LayoutTemplateOption {
  readonly id: LayoutTemplate;
  readonly name: string;
  readonly description: string;
  readonly previewUrl: string;
}

export const LAYOUT_TEMPLATES: readonly LayoutTemplateOption[] = [
  {
    id: 'classic',
    name: 'Classic',
    description: 'Clean and familiar',
    previewUrl: classicPreview,
  },
  {
    id: 'modern',
    name: 'Modern',
    description: 'Accent rule, compact',
    previewUrl: modernPreview,
  },
  {
    id: 'formal',
    name: 'Formal',
    description: 'Serif, centred name',
    previewUrl: formalPreview,
  },
];
