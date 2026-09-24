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
