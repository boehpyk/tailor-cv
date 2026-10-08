import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { LAYOUT_TEMPLATES } from '../layouts';
import { LayoutPicker } from './LayoutPicker';

import type { LayoutTemplate } from '../types';

/**
 * T26 (test-after) — slice 3.2: AC-29 (client mirror), AC-34 (a11y), AC-35 (no new dependency),
 * AC-38 frontend half (preview assets). jsdom has no layout engine, so "wraps at 360 px with no
 * horizontal scroll" is asserted as MARKUP (flex-wrap container, shrinkable cards, `min-w-0`
 * fieldset), not as a measured width.
 */

const HERE = dirname(fileURLToPath(import.meta.url));

function Harness(): React.JSX.Element {
  const [value, setValue] = useState<LayoutTemplate>('classic');
  return <LayoutPicker value={value} onChange={setValue} disabled={false} busy={false} />;
}

describe('AC-29: the client mirror of LayoutTemplate', () => {
  it('lists exactly classic, modern, formal, in that order', () => {
    expect(
      LAYOUT_TEMPLATES.map((t) => t.id),
      'LAYOUT_TEMPLATES drifted from LayoutTemplate in api/src/tailorcraft/domain/export/value_objects.py — change both together',
    ).toEqual(['classic', 'modern', 'formal']);
  });
});

describe('AC-34: the picker is operable and perceivable without a pointer or colour', () => {
  it('arrow keys move and select within the radio group', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const classic = screen.getByRole('radio', { name: 'Classic' });
    expect(classic).toBeChecked();
    classic.focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('radio', { name: 'Modern' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Modern' })).toHaveFocus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('radio', { name: 'Formal' })).toBeChecked();
    await user.keyboard('{ArrowLeft}');
    expect(screen.getByRole('radio', { name: 'Modern' })).toBeChecked();
  });

  it('every radio is described by its own description text', () => {
    render(<Harness />);
    for (const t of LAYOUT_TEMPLATES) {
      expect(screen.getByRole('radio', { name: t.name })).toHaveAccessibleDescription(
        t.description,
      );
    }
  });

  it('previews are decorative: alt is empty, so the name and description carry the meaning', () => {
    const { container } = render(<Harness />);
    const images = container.querySelectorAll('img');
    expect(images).toHaveLength(3);
    images.forEach((img) => {
      expect(img.getAttribute('alt')).toBe('');
    });
  });

  it('says "Selected" in text on the chosen card only, not by colour alone', () => {
    render(<Harness />);
    expect(screen.getAllByText('Selected')).toHaveLength(1);
    fireEvent.click(screen.getByRole('radio', { name: 'Formal' }));
    const selected = screen.getByText('Selected');
    expect(selected.closest('label')).toContainElement(
      screen.getByRole('radio', { name: 'Formal' }),
    );
  });

  it('shows a focus ring on the card when its radio has keyboard focus', () => {
    render(<Harness />);
    const card = screen.getByRole('radio', { name: 'Classic' }).closest('label');
    expect(card?.className).toContain('has-focus-visible:outline-2');
  });

  it('wraps rather than overflows at 360 px (markup, not measured: jsdom has no layout)', () => {
    render(<Harness />);
    const radio = screen.getByRole('radio', { name: 'Classic' });
    const cards = radio.closest('div');
    expect(cards?.className).toContain('flex-wrap');
    expect(radio.closest('fieldset')?.className).toContain('min-w-0');
    // basis-24 (6 rem) + the gap: three cards wrap onto a second row long before 360 px.
    expect(radio.closest('label')?.className).toContain('basis-24');
  });
});

describe('AC-35: no new dependency', () => {
  it('web/package.json lists exactly the dependencies of bfb98a9', () => {
    const pkg = JSON.parse(readFileSync(resolve(HERE, '../../../../package.json'), 'utf8')) as {
      dependencies: Record<string, string>;
      devDependencies: Record<string, string>;
    };
    expect(Object.keys(pkg.dependencies).sort()).toEqual([
      '@tanstack/react-query',
      '@tiptap/core',
      '@tiptap/extension-link',
      '@tiptap/pm',
      '@tiptap/react',
      '@tiptap/starter-kit',
      'markdown-it',
      'prosemirror-markdown',
      'react',
      'react-dom',
      'react-router',
    ]);
    expect(Object.keys(pkg.devDependencies).sort()).toEqual([
      '@eslint/js',
      '@tailwindcss/vite',
      '@testing-library/jest-dom',
      '@testing-library/react',
      '@testing-library/user-event',
      '@types/markdown-it',
      '@types/node',
      '@types/react',
      '@types/react-dom',
      '@vitejs/plugin-react',
      'eslint',
      'eslint-plugin-react-hooks',
      'eslint-plugin-react-refresh',
      'globals',
      'jsdom',
      'prettier',
      'tailwindcss',
      'typescript',
      'typescript-eslint',
      'vite',
      'vitest',
    ]);
  });
});

describe('AC-38 (frontend half): previews are hashed assets, lazy, and size-reserved', () => {
  it('vite.config.ts never inlines a .webp and leaves other assets on the default', () => {
    // vite.config cannot be imported under jsdom (esbuild refuses) and eval is banned, so this is a
    // SOURCE-shape pin on the policy: .webp -> false (never inline), anything else -> undefined
    // (Vite's default). Not a build of dist/; the T31 manual pass checks the built output.
    const source = readFileSync(resolve(HERE, '../../../../vite.config.ts'), 'utf8');
    expect(source).toMatch(
      /assetsInlineLimit:\s*\(\w+\)\s*=>\s*\(\w+\.endsWith\('\.webp'\)\s*\?\s*false\s*:\s*undefined\)/,
    );
  });

  it('each preview is a file URL (not a data: URI), lazy-loaded, with width and height', () => {
    const { container } = render(<Harness />);
    for (const t of LAYOUT_TEMPLATES) {
      expect(t.previewUrl).not.toMatch(/^data:/);
    }
    container.querySelectorAll('img').forEach((img) => {
      expect(img.getAttribute('loading')).toBe('lazy');
      expect(Number(img.getAttribute('width'))).toBeGreaterThan(0);
      expect(Number(img.getAttribute('height'))).toBeGreaterThan(0);
    });
  });
});
