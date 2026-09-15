import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * F12 — two structural guards enforced by walking the real `web/src` tree with `node:fs`, the way
 * AC-27 and the privacy check (Constitution §8) literally state them: "`grep -r
 * dangerouslySetInnerHTML web/src` matches only comments" and "not in `localStorage`,
 * `sessionStorage` or IndexedDB (a test greps)". A docstring claiming either property is not the
 * property; running the grep is.
 *
 * **This file is excluded from its own search.** Both needles appear in this file's own source —
 * as the strings being searched for — which would otherwise be a permanent false positive.
 */

const THIS_FILE = fileURLToPath(import.meta.url);
const SRC_ROOT = dirname(THIS_FILE);
const SOURCE_EXTENSIONS = new Set(['.ts', '.tsx']);
const SKIP_DIRECTORIES = new Set(['node_modules', 'dist']);

interface SourceLine {
  readonly path: string;
  readonly lineNumber: number;
  readonly text: string;
}

function collectSourceLines(dir: string): SourceLine[] {
  const lines: SourceLine[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stats = statSync(full);
    if (stats.isDirectory()) {
      if (!SKIP_DIRECTORIES.has(entry)) {
        lines.push(...collectSourceLines(full));
      }
      continue;
    }
    if (full === THIS_FILE || !SOURCE_EXTENSIONS.has(extname(full))) {
      continue;
    }
    readFileSync(full, 'utf-8')
      .split('\n')
      .forEach((text, index) => {
        lines.push({ path: full, lineNumber: index + 1, text });
      });
  }
  return lines;
}

/**
 * A line whose non-whitespace content is a comment: `//…`, a block-comment opener `/*…`, or a
 * JSDoc continuation line (` * …`). Every block comment in this codebase's style puts a leading `*`
 * on each continuation line (see any file under `features/`), which is what makes this check exact
 * enough for a repo-wide convention this consistent, without being a general TS tokenizer.
 */
function isCommentLine(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith('//') || trimmed.startsWith('/*') || trimmed.startsWith('*');
}

function isTestFile(path: string): boolean {
  return path.endsWith('.test.ts') || path.endsWith('.test.tsx');
}

function findNonCommentOccurrences(needle: string, lines: readonly SourceLine[]): string[] {
  return lines
    .filter((line) => line.text.includes(needle) && !isCommentLine(line.text))
    .map((line) => `${line.path}:${String(line.lineNumber)}: ${line.text.trim()}`);
}

describe('AC-27: no HTML is ever rendered from untrusted content', () => {
  it('dangerouslySetInnerHTML appears only in comments across web/src', () => {
    const lines = collectSourceLines(SRC_ROOT);

    expect(findNonCommentOccurrences('dangerouslySetInnerHTML', lines)).toEqual([]);
  });
});

describe('the editor document lives only in ProseMirror state, never browser storage', () => {
  it('localStorage, sessionStorage and indexedDB never appear in production code', () => {
    const lines = collectSourceLines(SRC_ROOT).filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('localStorage', lines)).toEqual([]);
    expect(findNonCommentOccurrences('sessionStorage', lines)).toEqual([]);
    expect(findNonCommentOccurrences('indexedDB', lines)).toEqual([]);
  });
});
