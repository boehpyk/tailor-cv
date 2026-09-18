import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * F12 — structural guards enforced by walking the real `web/src` tree with `node:fs`, the way
 * AC-27 and the privacy check (Constitution §8) literally state them: "`grep -r
 * dangerouslySetInnerHTML web/src` matches only comments" and "not in `localStorage`,
 * `sessionStorage` or IndexedDB (a test greps)". A docstring claiming either property is not the
 * property; running the grep is.
 *
 * **F8 (slice 1.5) extends this file to `features/export/` and adds a third guard, AC-42's "no
 * `<a href>` to an API URL exists".** The first two needed no code change to cover the new folder:
 * `SRC_ROOT` is `web/src` itself and `collectSourceLines` walks it recursively, so
 * `features/export/**` was already inside the swept tree the moment it existed — verified at F8 by
 * planting each forbidden pattern in a scratch file under `features/export/` and watching the
 * existing tests go red on it, then removing it. The third guard is genuinely new: no test grepped
 * for `href="/api/` before this slice had an endpoint worth downloading from directly.
 *
 * **This file is excluded from its own search.** All three needles appear in this file's own
 * source — as the strings being searched for — which would otherwise be a permanent false
 * positive.
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

describe('AC-42: a download never goes through a raw anchor to the API', () => {
  it('href="/api/ never appears in production code', () => {
    // Test files excluded for the same reason `localStorage` excludes them: a test's own
    // description string can legitimately name the forbidden pattern in prose (AC-42's own tests
    // say "never an <a href=\"/api/…\">" in their `it(...)` titles) without a real anchor existing
    // anywhere. What this guard polices is the JSX the app ships, where `requestBlob`'s docstring
    // says exactly why an anchor to this path is dangerous: it 401s into a JSON error saved under
    // a `.pdf` name.
    const lines = collectSourceLines(SRC_ROOT).filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('href="/api/', lines)).toEqual([]);
  });
});
