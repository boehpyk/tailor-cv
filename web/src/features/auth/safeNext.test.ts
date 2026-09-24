import { describe, expect, it } from 'vitest';

import { safeNext } from './safeNext';

/**
 * T37 RED (AC-43, I-52) — `safeNext` over every case the technical plan and the feature spec name,
 * written against `safeNext.ts`'s own docstring before the function has a body (T36 skeleton
 * throws). The rule is an allow-list of one shape (a same-origin path), so every case that is not
 * that shape must fall back to `/`, and every case that is must come back byte-for-byte unchanged
 * (a query string and a fragment are part of the path, not something to strip).
 */

interface Case {
  readonly name: string;
  readonly input: string | null | undefined;
  readonly expected: string;
}

const CASES: readonly Case[] = [
  { name: '"/" is already the safe default and is returned unchanged', input: '/', expected: '/' },
  {
    name: 'a plain same-origin path is returned unchanged',
    input: '/account',
    expected: '/account',
  },
  {
    name: 'a query string and a fragment on a safe path are kept, not stripped',
    input: '/account?x=1#y',
    expected: '/account?x=1#y',
  },
  {
    name: 'protocol-relative "//evil.example" resolves to another host in a browser — falls back',
    input: '//evil.example',
    expected: '/',
  },
  {
    name: '"/\\\\evil.example" is normalised to "//evil.example" by a special-scheme URL parser — falls back',
    input: '/\\evil.example',
    expected: '/',
  },
  {
    name: 'an absolute URL to another origin falls back',
    input: 'https://evil.example',
    expected: '/',
  },
  {
    name: 'a "javascript:" URL is not a path at all — falls back',
    input: 'javascript:alert(1)',
    expected: '/',
  },
  { name: 'the empty string asked for nothing — falls back', input: '', expected: '/' },
  { name: 'null asked for nothing — falls back', input: null, expected: '/' },
  { name: 'undefined asked for nothing — falls back', input: undefined, expected: '/' },
  {
    name: 'a leading space is not the start of a path — falls back (not trimmed and accepted)',
    input: ' /account',
    expected: '/',
  },
  // T44 (`qa`, test-after) — AC-43/I-52: a control character between the leading "/" and a second
  // "/" would otherwise slip past the regex's `(?![/\\])` lookahead — a tab, CR or LF is neither
  // "/" nor "\", so `/^\/(?![/\\])/` alone accepts `/\t/evil.example`. The WHATWG URL parser
  // *strips* ASCII tab/CR/LF before it parses anything (the "C0 control or space" trim step in the
  // URL parsing spec applies to the whole input, not just the ends), so a browser resolving this
  // "safe" path sees exactly `//evil.example` — protocol-relative, another host. `safeNext.ts`'s own
  // docstring names this case; these three assert it, one control character at a time, so the guard
  // cannot regress to matching only the WHATWG spec's own worked example.
  {
    name: 'a tab immediately after the leading "/" would parse as "//evil.example" — falls back',
    input: '/\t/evil.example',
    expected: '/',
  },
  {
    name: 'a line feed immediately after the leading "/" would parse as "//evil.example" — falls back',
    input: '/\n/evil.example',
    expected: '/',
  },
  {
    name: 'a carriage return immediately after the leading "/" would parse as "//evil.example" — falls back',
    input: '/\r/evil.example',
    expected: '/',
  },
];

describe('safeNext', () => {
  it.each(CASES)('$name', ({ input, expected }) => {
    expect(safeNext(input)).toBe(expected);
  });

  it('never throws, whatever it is given', () => {
    for (const { input } of CASES) {
      expect(() => safeNext(input)).not.toThrow();
    }
  });
});
