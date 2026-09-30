import { act, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

/**
 * T33 — AC-51 / H-57 (OQ-12): typing into `/register` or `/login` while the boot refresh is still
 * in flight must not be lost when it settles `anonymous`.
 *
 * **A regression guard written after the fact, with no recorded red.** AC-51 asked for 2.2's
 * `/verify` observation (text wiped by a remount during the boot refresh) to be reproduced red
 * first. It did not reproduce — neither here nor in a probe running the real route table under
 * `<StrictMode>`: none of `RegisterPage`, `LoginPage` or `CredentialsForm` has a `booting` branch
 * (unchanged since `d0714e1`), so the remount the plan diagnosed in §7 is not in the code. This test
 * pins that it stays so; a real-browser attempt at the original observation is `/verify`'s manual
 * pass on `:8080`.
 *
 * The boot refresh is held open by a deferred response; the user types into both fields; the
 * refresh then answers 401 `not_signed_in` (a boot 401 is "no login to resume" → `anonymous`), and
 * the characters must still be in the inputs.
 */

const EMAIL = 'typed-while-booting@example.com';
const PASSWORD = 'typed while booting 12';

let settleBoot: (response: Response) => void = () => undefined;

beforeEach(() => {
  __resetForTests();
  stubAccountFetch({
    'POST /api/auth/refresh': () =>
      new Promise<Response>((resolve) => {
        settleBoot = resolve;
      }),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Boot refresh does not remount the credentials form (AC-51)', () => {
  it.each(['/register', '/login'])(
    'what is typed into %s while the boot refresh is pending survives it settling anonymous',
    async (path) => {
      const boot = authStore.bootstrap();
      const user = userEvent.setup();

      renderWithRouter(path);
      expect(authStore.getSnapshot().status).toBe('booting');
      await user.type(await screen.findByLabelText('Email'), EMAIL);
      await user.type(screen.getByLabelText('Password'), PASSWORD);

      await act(async () => {
        settleBoot(jsonResponse(401, { error: { code: 'not_signed_in', message: 'no login' } }));
        await boot;
      });

      expect(authStore.getSnapshot().status).toBe('anonymous');
      expect(screen.getByLabelText('Email')).toHaveValue(EMAIL);
      expect(screen.getByLabelText('Password')).toHaveValue(PASSWORD);
    },
  );
});
