import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * AC-7 (web-design-refresh, ADR-0037): the design lives in `@theme` tokens and utilities only.
 * Walks the real `web/src` tree, like `security.grep.test.ts` (same walker idiom, copied rather
 * than shared because that file is not a module and owns its own exclusions).
 *
 * Half 1, production `.tsx` only: no `style={`, no hex colour literal, no `oklch(`/`rgb(`, no
 * arbitrary colour class (`-[#…`, `-[oklch…`).
 *
 * Half 2, production `.ts` AND `.tsx` (illustration copy will live in a `.ts` module): the fictional
 * names appear only under `src/features/marketing/`. Vacuous until that folder exists — the
 * discrimination controls below prove the predicates bite regardless.
 *
 * **Hex literal shape (decision).** `#` + 3, 4, 6 or 8 hex digits, not followed by a word character
 * or `-`, and not directly after `href="` / `to="` — so an in-page anchor such as `href="#add"` or
 * `to="#fee"` (all-hex words) is not flagged, while `bg-[#fff]`, `color: '#1a2b3c'` and `fill="#000"`
 * are. HTML entities (`&#39;`) are not flagged: `&` precedes the `#`. Comment lines are skipped.
 */

const THIS_FILE = fileURLToPath(import.meta.url);
const SRC_ROOT = dirname(THIS_FILE);
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
    if (statSync(full).isDirectory()) {
      if (!SKIP_DIRECTORIES.has(entry)) {
        lines.push(...collectSourceLines(full));
      }
      continue;
    }
    if (full === THIS_FILE || !['.ts', '.tsx'].includes(extname(full))) {
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

function isCommentLine(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith('//') || trimmed.startsWith('/*') || trimmed.startsWith('*');
}

function isTestFile(path: string): boolean {
  return /[\\/]test[\\/]/.test(path) || path.endsWith('.test.ts') || path.endsWith('.test.tsx');
}

const HEX_LITERAL =
  /(?<!(?:href|to)=["'])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/;

const FORBIDDEN: readonly { readonly label: string; readonly test: (text: string) => boolean }[] = [
  { label: 'style={', test: (t) => t.includes('style={') },
  { label: 'hex colour literal', test: (t) => HEX_LITERAL.test(t) },
  { label: 'oklch(', test: (t) => t.includes('oklch(') },
  { label: 'rgb(', test: (t) => t.includes('rgb(') },
  { label: 'arbitrary colour class -[#', test: (t) => t.includes('-[#') },
  { label: 'arbitrary colour class -[oklch', test: (t) => t.includes('-[oklch') },
];

const ILLUSTRATION_NAMES = ['Maya Okafor', 'Okafor', 'Northwind', 'Fieldnote'] as const;
const MARKETING_DIRECTORY = /[\\/]features[\\/]marketing[\\/]/;

function format(line: SourceLine): string {
  return `${line.path}:${String(line.lineNumber)}: ${line.text.trim()}`;
}

function findForbiddenDesign(label: string, lines: readonly SourceLine[]): string[] {
  const rule = FORBIDDEN.find((candidate) => candidate.label === label);
  if (rule === undefined) {
    throw new Error(`unknown rule ${label}`);
  }
  return lines.filter((l) => !isCommentLine(l.text) && rule.test(l.text)).map(format);
}

function findOutsideMarketing(needle: string, lines: readonly SourceLine[]): string[] {
  return lines
    .filter((l) => !MARKETING_DIRECTORY.test(l.path) && l.text.includes(needle))
    .map(format);
}

const productionTsx = (): SourceLine[] =>
  collectSourceLines(SRC_ROOT).filter((l) => l.path.endsWith('.tsx') && !isTestFile(l.path));
const productionTs = (): SourceLine[] =>
  collectSourceLines(SRC_ROOT).filter((l) => !isTestFile(l.path));

describe('AC-7: no inline style or colour literal in production .tsx', () => {
  it('the scan reads production .tsx files (positive control)', () => {
    expect(productionTsx().length).toBeGreaterThan(100);
  });

  it.each(FORBIDDEN.map((rule) => rule.label))('%s never appears', (label) => {
    expect(findForbiddenDesign(label, productionTsx())).toEqual([]);
  });

  it('discriminates: flags each colour shape and spares anchors, entities and comments', () => {
    const flagged = [
      '<p style={{ color: "red" }} />',
      'className="bg-[#fff]"',
      "const c = '#1a2b3c';",
      '<path fill="#000" />',
      'x = "#ABCD1234"',
      'const c = "oklch(0.5 0.1 200)";',
      'const c = "rgb(0 0 0)";',
      'className="text-[oklch(0.5_0.1_200)]"',
    ];
    const spared = [
      '<a href="#pricing">',
      '<a href="#how-it-works">',
      '<a href="#add">',
      '<Link to="#fee">',
      'it&#39;s',
      '// style={ is only a comment',
    ];
    const hits = (text: string): number =>
      FORBIDDEN.filter((rule) => !isCommentLine(text) && rule.test(text)).length;

    for (const text of flagged) {
      expect(hits(text), text).toBeGreaterThan(0);
    }
    for (const text of spared) {
      expect(hits(text), text).toBe(0);
    }
  });
});

describe('AC-7: illustration names live only under features/marketing/', () => {
  it('the scan reads production .ts copy modules as well as .tsx (positive control)', () => {
    const paths = productionTs().map((l) => l.path);

    expect(paths.some((p) => p.endsWith('.ts'))).toBe(true);
    expect(paths.some((p) => p.endsWith('.tsx'))).toBe(true);
  });

  it.each(ILLUSTRATION_NAMES)('%s appears nowhere outside features/marketing/', (needle) => {
    expect(findOutsideMarketing(needle, productionTs())).toEqual([]);
  });

  it('discriminates: a hit outside marketing is flagged, one inside is not', () => {
    const planted: SourceLine[] = [
      { path: '/src/features/history/Copy.ts', lineNumber: 1, text: "name: 'Maya Okafor'" },
      { path: '/src/features/marketing/marketingCopy.ts', lineNumber: 1, text: "'Maya Okafor'" },
    ];

    expect(findOutsideMarketing('Okafor', planted)).toHaveLength(1);
  });
});
