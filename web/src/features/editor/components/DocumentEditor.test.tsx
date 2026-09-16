import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { useDocumentEditor } from '../hooks/useDocumentEditor';
import { DocumentEditor } from './DocumentEditor';

import type { Editor } from '@tiptap/core';

/**
 * F12 — structure and markup for `DocumentEditor`'s toolbar (task-list F12): every control is a
 * `type="button"` carrying `aria-pressed`, and pressing one toggles that attribute through the
 * **real** editor.
 *
 * Deliberately not a hand-built `handle` with a stubbed `editor` — the component's own docstring
 * explains why `aria-pressed` is read through `useEditorState`'s subscription rather than a plain
 * `editor.isActive` call in render: a transaction that changes what is active at the cursor changes
 * nothing React knows about on its own. A mock editor whose `isActive` is a fixed `vi.fn()` could
 * never exercise that subscription; only a live instance whose selection genuinely changes can.
 * `useDocumentEditor` is the real hook (F8/F10a), seeded through the real Markdown bridge, exactly
 * as `DocumentWorkspace` uses it.
 */

const TOOLBAR_LABELS = [
  'Bold',
  'Italic',
  'Heading 1',
  'Heading 2',
  'Heading 3',
  'Bullet list',
  'Numbered list',
] as const;

let capturedEditor: Editor | null = null;

function Harness(): React.JSX.Element {
  const handle = useDocumentEditor('Some tailored text to select.', { editable: true });
  capturedEditor = handle.editor;
  return <DocumentEditor handle={handle} kind="cv" hidden={false} />;
}

function currentEditor(): Editor {
  if (capturedEditor === null) {
    throw new Error('editor was not created');
  }
  return capturedEditor;
}

describe('DocumentEditor toolbar', () => {
  it('renders all seven controls as type="button" carrying aria-pressed, unpressed with no selection', () => {
    render(<Harness />);

    for (const label of TOOLBAR_LABELS) {
      const button = screen.getByRole('button', { name: label });
      expect(button).toHaveAttribute('type', 'button');
      expect(button).toHaveAttribute('aria-pressed', 'false');
    }
  });

  it('pressing Bold on a selection toggles aria-pressed, through the real editor', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const editor = currentEditor();

    act(() => {
      editor.commands.selectAll();
    });

    const boldButton = screen.getByRole('button', { name: 'Bold' });
    expect(boldButton).toHaveAttribute('aria-pressed', 'false');
    expect(editor.isActive('bold')).toBe(false);

    await user.click(boldButton);

    expect(editor.isActive('bold')).toBe(true);
    expect(boldButton).toHaveAttribute('aria-pressed', 'true');

    await user.click(boldButton);

    expect(editor.isActive('bold')).toBe(false);
    expect(boldButton).toHaveAttribute('aria-pressed', 'false');
  });
});
