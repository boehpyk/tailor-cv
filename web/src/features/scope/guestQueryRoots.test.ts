/// <reference types="vite/client" />

import { describe, expect, it } from 'vitest';

import { GUEST_SCOPE_MAP, GUEST_QUERY_ROOTS } from './scopeMap';

/**
 * T34 (`qa`, test-after) — AC-45 / plan §0.11: **every guest query-key builder's first element is
 * in `GUEST_QUERY_ROOTS`**, so a claim's `removeQueries` over those roots cannot leave a guest
 * cache entry behind.
 *
 * **Discovered, not listed.** Every module under `features/**\/hooks/*.ts` is imported and each
 * export whose name ends `Key` or `KeyPrefix` (mutation keys excluded: the mutation cache is not
 * the query cache) is a builder — a constant array, or a function called with whichever argument
 * shape it takes (none, a string id, a scope map, or both). Builders are all in `hooks/` by this
 * codebase's convention; a key written inline in a component would not be found, which is why the
 * hooks themselves reference builders (`grep queryKey:` shows no inline array literal). A builder
 * that cannot be called with any of those shapes fails the test by name rather than being skipped.
 *
 * `NON_GUEST_ROOTS` is the explicit allowance: `auth` (identity and account data, cleared by 2.1's
 * sign-out) and `health` (the readiness probe; no user data). A new first segment fails here until
 * someone puts it in one list or the other **on purpose**.
 *
 * Mutations (restored byte-exact): `export const probeQueryKey = ['probe', 'x'] as const;` added to
 * `features/posting/hooks/useJobPostings.ts` → "every builder's root is declared" red, naming
 * `probeQueryKey` with root `probe`. Removing `['export']` from `GUEST_QUERY_ROOTS` → red naming
 * `exportJobsQueryKey`. Both green on restore.
 */

const NON_GUEST_ROOTS: readonly string[] = ['auth', 'health'];

const modules: Record<string, Record<string, unknown>> = import.meta.glob(
  ['../**/hooks/*.ts', '!../**/*.test.ts'],
  { eager: true },
);

const ARGUMENT_SHAPES: readonly (readonly unknown[])[] = [
  [],
  ['probe-id'],
  [GUEST_SCOPE_MAP],
  ['probe-id', GUEST_SCOPE_MAP],
];

interface Builder {
  readonly name: string;
  readonly key: readonly unknown[] | null;
}

function evaluate(value: unknown): readonly unknown[] | null {
  if (Array.isArray(value)) {
    return value as readonly unknown[];
  }
  if (typeof value !== 'function') {
    return null;
  }
  for (const args of ARGUMENT_SHAPES) {
    try {
      const out: unknown = (value as (...a: unknown[]) => unknown)(...args);
      if (Array.isArray(out)) {
        return out as readonly unknown[];
      }
    } catch {
      // try the next argument shape
    }
  }
  return null;
}

function discoverBuilders(
  source: Readonly<Record<string, Readonly<Record<string, unknown>>>>,
): Builder[] {
  const found: Builder[] = [];
  for (const exports of Object.values(source)) {
    for (const [name, value] of Object.entries(exports)) {
      if (/Key(Prefix)?$/.test(name) && !/MutationKey$/.test(name)) {
        found.push({ name, key: evaluate(value) });
      }
    }
  }
  return found;
}

/** The problems with a set of builders, as sentences — empty when every root is declared. */
function rootProblems(builders: readonly Builder[], guestRoots: readonly string[]): string[] {
  return builders.flatMap(({ name, key }) => {
    if (key === null) {
      return [`${name}: could not be evaluated with any known argument shape`];
    }
    const root = key[0];
    if (typeof root !== 'string') {
      return [`${name}: first element is not a string`];
    }
    return guestRoots.includes(root) || NON_GUEST_ROOTS.includes(root)
      ? []
      : [`${name}: root "${root}" is in neither GUEST_QUERY_ROOTS nor NON_GUEST_ROOTS`];
  });
}

const GUEST_ROOT_NAMES = GUEST_QUERY_ROOTS.map(([root]) => root);

describe('AC-45: every guest query-key builder starts with a declared root', () => {
  const builders = discoverBuilders(modules);

  it('discovery finds the four known guest builders (positive control)', () => {
    const names = builders.map((b) => b.name);
    for (const known of [
      'baseCvsQueryKey',
      'jobPostingsQueryKey',
      'tailoringRunsQueryKey',
      'tailoringRunQueryKey',
      'exportJobsQueryKey',
    ]) {
      expect(names).toContain(known);
    }
  });

  it("every builder's root is declared in GUEST_QUERY_ROOTS or NON_GUEST_ROOTS", () => {
    expect(rootProblems(builders, GUEST_ROOT_NAMES)).toEqual([]);
  });

  it('every GUEST_QUERY_ROOTS entry is the root of at least one builder (no dead root)', () => {
    const used = new Set(builders.map((b) => b.key?.[0]));
    expect(GUEST_ROOT_NAMES.filter((root) => !used.has(root))).toEqual([]);
  });

  it('the checker discriminates: a synthetic fifth root, and an unevaluable builder, are flagged', () => {
    const problems = rootProblems(
      [
        { name: 'fifthQueryKey', key: ['fifth', 'x'] },
        { name: 'brokenKey', key: null },
        { name: 'okQueryKey', key: ['intake', 'baseCvs'] },
      ],
      GUEST_ROOT_NAMES,
    );

    expect(problems).toHaveLength(2);
    expect(problems[0]).toContain('fifthQueryKey');
    expect(problems[1]).toContain('brokenKey');
  });
});
