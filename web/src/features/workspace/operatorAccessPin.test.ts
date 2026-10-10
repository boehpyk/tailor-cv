import { describe, expect, it } from 'vitest';

import { ACCOUNT_PROMISE, OPERATOR_ACCESS_NOTE } from './workspaceCopy';

/**
 * T22 — AC-37's verbatim pin. The sentence is OQ-14's, approved 2026-10-10 and quoted in
 * Constitution §8; it is spelled out here so editing the constant alone turns this red.
 */
const OQ_14_SENTENCE =
  'The person who runs TailorCraft can read what is stored here — CVs, job postings and tailored documents — to operate and support the service.';

describe('the operator-access sentence — AC-37', () => {
  it('OPERATOR_ACCESS_NOTE equals the approved sentence exactly', () => {
    expect(OPERATOR_ACCESS_NOTE).toBe(OQ_14_SENTENCE);
  });

  it('the account promise ends with it, once', () => {
    expect(ACCOUNT_PROMISE.endsWith(` ${OQ_14_SENTENCE}`)).toBe(true);
    expect(ACCOUNT_PROMISE.split(OQ_14_SENTENCE)).toHaveLength(2);
  });
});
