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

/**
 * T44 (`qa`, test-after) — AC-35: the access token lives only in `authStore`'s module scope
 * (`useSyncExternalStore`), never in browser storage, a readable cookie, or the TanStack Query
 * cache. The broader "no localStorage/sessionStorage in production code" guard above already
 * sweeps all of `web/src`, which is a superset of `features/auth/` and `api/` — but AC-35 names
 * those two directories specifically (they are where a token could plausibly end up), so this
 * scopes the same guards there explicitly and adds `document.cookie`, which the broader guard above
 * does not check at all (ADR-0008: the *refresh* token is a `HttpOnly` cookie the client cannot and
 * must not read; a `document.cookie` read anywhere in this pair of directories would mean an attempt
 * to read it, or to persist the access token the same way).
 */

const AUTH_SCOPED_DIRECTORY_PATTERN = /[\\/](?:features[\\/]auth|api)[\\/]/;

/** Only source lines under `web/src/features/auth/` or `web/src/api/`. */
function collectAuthScopedLines(): SourceLine[] {
  return collectSourceLines(SRC_ROOT).filter((line) =>
    AUTH_SCOPED_DIRECTORY_PATTERN.test(line.path),
  );
}

describe('AC-35: the access token is never in browser storage or a readable cookie (features/auth/, api/)', () => {
  it('localStorage never appears in features/auth/ or api/ production code', () => {
    const lines = collectAuthScopedLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('localStorage', lines)).toEqual([]);
  });

  it('sessionStorage never appears in features/auth/ or api/ production code', () => {
    const lines = collectAuthScopedLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('sessionStorage', lines)).toEqual([]);
  });

  it('document.cookie never appears in features/auth/ or api/ production code', () => {
    const lines = collectAuthScopedLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('document.cookie', lines)).toEqual([]);
  });
});

/**
 * AC-35's second half: the token must never reach `useState` (a component copy TanStack Query's own
 * rule already forbids for server state, and doubly wrong for a credential), `setQueryData` (the
 * query cache is for the *user profile*, `['auth', 'me'], never the token — `authCache.ts`'s own
 * docstring says so) or `useQuery` (which would make the token subject to refetch-on-focus, retry
 * and garbage collection, exactly the properties `authStore.ts`'s docstring gives as the reason the
 * token is *not* server state).
 *
 * This is a co-occurrence check, same line, same convention as `findNonCommentOccurrences` above
 * (a literal/regex substring search, not an AST parse) — deliberately narrower than "anywhere in the
 * file", because `accessToken` legitimately appears on lines that have nothing to do with any of
 * these three calls (e.g. `accessToken: grant.accessToken` in `authMachine.ts`, or
 * `const sentToken = await authStore.accessTokenForRequest()` in `client.ts`) and flagging those
 * would make the guard noisy enough to be worked around rather than trusted.
 */
const TOKEN_NAME_PATTERN = /\baccess_token\b|\baccessToken\b/;
const LEAKING_CALL_PATTERN = /\b(?:useState|setQueryData|useQuery)\s*\(/;

function isAccessTokenLeakLine(text: string): boolean {
  return TOKEN_NAME_PATTERN.test(text) && LEAKING_CALL_PATTERN.test(text);
}

function findAccessTokenLeaks(lines: readonly SourceLine[]): string[] {
  return lines
    .filter((line) => !isCommentLine(line.text) && isAccessTokenLeakLine(line.text))
    .map((line) => `${line.path}:${String(line.lineNumber)}: ${line.text.trim()}`);
}

describe('AC-35: the access token never flows into useState, setQueryData or useQuery (features/auth/, api/)', () => {
  it('discriminates: flags the token reaching each of the three calls, and does not flag ordinary uses of the token or of those calls alone', () => {
    // Proven against synthetic strings, not a planted scratch file — `isAccessTokenLeakLine` is a
    // same-line predicate, so a string is enough to exercise every branch without touching disk.
    const violations = [
      'const [token, setToken] = useState(accessToken);',
      "queryClient.setQueryData(['auth', 'token'], access_token);",
      "useQuery({ queryKey: ['auth', 'token'], queryFn: () => accessToken });",
    ];
    const innocentTokenUses = [
      'accessToken: grant.accessToken,', // authMachine.ts — a plain object field, no call in sight
      'const sentToken = await authStore.accessTokenForRequest();', // client.ts
      '  return { kind: "ok" as const, accessToken: response.access_token };',
    ];
    const innocentCallUses = [
      "const [email, setEmail] = useState('');", // CredentialsForm.tsx — useState with no token
      'queryClient.setQueryData(currentUserQueryKey, result.user);', // authCache.ts — the profile, not the token
      'useQuery({ queryKey: currentUserQueryKey, queryFn: fetchMe });',
    ];

    for (const line of violations) {
      expect(isAccessTokenLeakLine(line)).toBe(true);
    }
    for (const line of [...innocentTokenUses, ...innocentCallUses]) {
      expect(isAccessTokenLeakLine(line)).toBe(false);
    }
  });

  it('no line in features/auth/ or api/ production code passes accessToken/access_token to useState, setQueryData or useQuery', () => {
    const lines = collectAuthScopedLines().filter((line) => !isTestFile(line.path));

    expect(findAccessTokenLeaks(lines)).toEqual([]);
  });
});

/**
 * T29 (`qa`, test-after) — AC-47: `features/savedCvs/` scoped explicitly, the same way AC-35 scopes
 * `features/auth/` and `api/` above, rather than resting on the fact that the broad, unscoped
 * checks earlier in this file already sweep all of `web/src` (this directory included, since
 * `SRC_ROOT` is `web/src` itself). A label or a filename is user-typed text a saved CV's owner
 * chose; a saved CV's server response is never handed to `dangerouslySetInnerHTML`, and none of a
 * saved CV's data — nor the access token this section's requests carry (`auth: 'required'`,
 * `api/savedBaseCvs.ts`) — is ever written to `localStorage`, `sessionStorage` or `indexedDB`.
 */
const SAVED_CVS_DIRECTORY_PATTERN = /[\\/]features[\\/]savedCvs[\\/]/;

function collectSavedCvsLines(): SourceLine[] {
  return collectSourceLines(SRC_ROOT).filter((line) => SAVED_CVS_DIRECTORY_PATTERN.test(line.path));
}

describe('AC-47: features/savedCvs/ never renders raw HTML or reaches into browser storage', () => {
  it('dangerouslySetInnerHTML never appears in features/savedCvs/ production code', () => {
    const lines = collectSavedCvsLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('dangerouslySetInnerHTML', lines)).toEqual([]);
  });

  it('localStorage never appears in features/savedCvs/ production code', () => {
    const lines = collectSavedCvsLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('localStorage', lines)).toEqual([]);
  });

  it('sessionStorage never appears in features/savedCvs/ production code', () => {
    const lines = collectSavedCvsLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('sessionStorage', lines)).toEqual([]);
  });

  it('indexedDB never appears in features/savedCvs/ production code', () => {
    const lines = collectSavedCvsLines().filter((line) => !isTestFile(line.path));

    expect(findNonCommentOccurrences('indexedDB', lines)).toEqual([]);
  });
});

/**
 * Slice 2.3 (T35, AC-50): the three folders 2.3 added — history, the workspace scope, and the
 * workspace (which now holds the account workspace) — get the same two guarantees as
 * `features/savedCvs/`: no raw HTML, and no document, posting or token in browser storage. Each
 * folder is checked on its own, with a positive control that the scan actually read files there, so
 * a renamed folder cannot make the absence assertions pass by scanning nothing.
 */
const SLICE_2_3_DIRECTORIES = ['history', 'scope', 'workspace'] as const;

function collectFeatureLines(feature: string): SourceLine[] {
  const pattern = new RegExp(`[\\\\/]features[\\\\/]${feature}[\\\\/]`);
  return collectSourceLines(SRC_ROOT).filter(
    (line) => pattern.test(line.path) && !isTestFile(line.path),
  );
}

describe.each(SLICE_2_3_DIRECTORIES)(
  'AC-50: features/%s/ never renders raw HTML or reaches into browser storage',
  (feature) => {
    it('the scan reads production files in this folder (positive control)', () => {
      expect(new Set(collectFeatureLines(feature).map((line) => line.path)).size).toBeGreaterThan(
        0,
      );
    });

    it.each([
      'dangerouslySetInnerHTML',
      'localStorage',
      'sessionStorage',
      'indexedDB',
      'document.cookie',
    ])('%s never appears in its production code', (needle) => {
      expect(findNonCommentOccurrences(needle, collectFeatureLines(feature))).toEqual([]);
    });
  },
);
