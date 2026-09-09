import { describe, expect, it } from 'vitest';

import { formatSize, formatStoredUntil } from './format';

describe('formatSize', () => {
  it('renders a sub-1024-byte count in bytes', () => {
    expect(formatSize(512)).toBe('512 B');
  });

  it('renders exactly 1024 bytes as 1 KB — the first value in the KB branch', () => {
    expect(formatSize(1024)).toBe('1 KB');
  });

  it('renders a whole number of kibibytes with no decimal', () => {
    expect(formatSize(2048)).toBe('2 KB');
  });

  it('renders a byte count at or above 1024 KiB in megabytes, to one decimal', () => {
    // 1440 KiB = 1,474,560 bytes; 1440 / 1024 = 1.40625 MiB, which the one-decimal MB branch
    // renders as "1.4 MB" — format.ts's own docblock example.
    expect(formatSize(1440 * 1024)).toBe('1.4 MB');
  });
});

describe('formatStoredUntil', () => {
  it('formats an ISO timestamp as day, short month and 24-hour time', () => {
    // technical-plan.md's success-state row states the target literally: for
    // `expires_at: "2026-09-08T10:00:00Z"` (the API contract's own example, line 336) the UI reads
    // "stored until 8 Sep 10:00" (line 402) — day first, no comma, 24-hour clock, no year. That is
    // the acceptance criterion this asserts, not whatever the runtime's default locale happens to
    // produce for `toLocaleString(undefined, …)`.
    expect(formatStoredUntil('2026-09-08T10:00:00Z')).toBe('8 Sep 10:00');
  });
});
