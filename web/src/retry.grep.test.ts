import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * T22 (proof; slice 3.3, AC-21) — **no write auto-retries.** A retried `POST` that timed out
 * server-side is a second tailoring run, a second export, a second account, and the second one is
 * paid for. So no `useMutation` options object may carry `retry`, and `main.tsx` may not give
 * mutations a default one. The single exception is `useDocumentAutosave.ts`: its `PUT` is
 * idempotent (the same text at the same version) and the autosave machine owns the outcome.
 *
 * Discovery, not a list: every non-test `.ts`/`.tsx` under `web/src` is scanned, each
 * `useMutation({ … })` literal is cut out by brace matching (comments stripped first), and a new
 * mutation anywhere is checked the day it is written.
 *
 * AC-16's "no raw Retry-After seconds in copy" scan is NOT repeated here: it lives in
 * `features/retry/accountHolds.test.tsx` ("no raw Retry-After seconds reach the copy").
 *
 * Mutation (5), recorded 2026-10-09: adding `retry: 1,` inside `useCreateTailoringRun`'s
 * `useMutation({ … })` turned the first test red:
 *   AssertionError: useMutation options with `retry`: ["features/tailoring/hooks/useCreateTailoringRun.ts"]: expected [ Array(1) ] to deeply equal []
 * Source restored byte-exact (`git diff` empty).
 */

const THIS_FILE = fileURLToPath(import.meta.url);
const SRC_ROOT = dirname(THIS_FILE);
const ALLOWED = 'features/editor/hooks/useDocumentAutosave.ts';

function sourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry !== 'node_modules' && entry !== 'dist') {
        files.push(...sourceFiles(full));
      }
    } else if (['.ts', '.tsx'].includes(extname(full)) && full !== THIS_FILE) {
      files.push(full);
    }
  }
  return files;
}

const shipped = sourceFiles(SRC_ROOT)
  .map((full) => relative(SRC_ROOT, full))
  .filter((path) => !/\.test\.tsx?$/.test(path) && !path.startsWith('test/'));

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

/** The text of each `useMutation({ … })` options literal in `text`, braces balanced. */
function mutationOptions(text: string): string[] {
  const found: string[] = [];
  const opener = /\buseMutation\s*(?:<[^()]*>)?\s*\(\s*\{/g;
  for (let match = opener.exec(text); match !== null; match = opener.exec(text)) {
    const start = match.index + match[0].length - 1;
    let depth = 0;
    for (let i = start; i < text.length; i += 1) {
      depth += text[i] === '{' ? 1 : text[i] === '}' ? -1 : 0;
      if (depth === 0) {
        found.push(text.slice(start, i + 1));
        break;
      }
    }
  }
  return found;
}

const RETRY_KEY = /(?:^|[\s,{])retry\s*[:,}]/;

describe('AC-21 — no write auto-retries', () => {
  const scanned = shipped.map((path) => ({
    path,
    options: mutationOptions(stripComments(readFileSync(join(SRC_ROOT, path), 'utf8'))),
  }));

  it('no useMutation options object carries `retry`, except the autosave PUT', () => {
    const offenders = scanned
      .filter(({ path, options }) => path !== ALLOWED && options.some((o) => RETRY_KEY.test(o)))
      .map(({ path }) => path);

    expect(offenders, `useMutation options with \`retry\`: ${JSON.stringify(offenders)}`).toEqual(
      [],
    );
  });

  it('controls: the scan sees the mutations, and the allowed exception really retries', () => {
    const calls = scanned.reduce((n, { options }) => n + options.length, 0);
    console.log(`useMutation call sites scanned: ${String(calls)}`);
    expect(calls).toBeGreaterThanOrEqual(20);
    const autosave = scanned.find(({ path }) => path === ALLOWED);
    expect(autosave?.options.some((o) => RETRY_KEY.test(o))).toBe(true);
    // The pattern can see a `retry` key at all (the planted case a regression would produce).
    expect(RETRY_KEY.test('{ mutationFn, retry: 1 }')).toBe(true);
    expect(RETRY_KEY.test('{ mutationFn, onError }')).toBe(false);
  });

  it('main.tsx gives mutations no default retry', () => {
    const main = stripComments(readFileSync(join(SRC_ROOT, 'main.tsx'), 'utf8'));

    expect(main).toMatch(/queries\s*:\s*\{[^}]*retry/); // control: the file does set a query retry
    expect(main).not.toMatch(/mutations\s*:\s*\{[^}]*retry/);
  });
});
