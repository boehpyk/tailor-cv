import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { createAppQueryClient } from '@/queryClient';
import { __resetForTests } from '../authStore';
import { REGISTER_GUEST_WORK_NOTICE, REGISTER_PANEL_HEADING } from '../authCopy';
import { RegisterPage } from './RegisterPage';

/**
 * AC-35 [proof] — the `/register` guest-work panel is static text: no input, no button, no link,
 * nothing ticked, and no request is made by rendering the page. What guest work is, and whether
 * to keep it, is asked after sign-in, by name (ADR-0025) — never pre-ticked here.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderRegister(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(() => Promise.reject(new Error('no request expected')));
  vi.stubGlobal('fetch', fetchMock);
  const client = createAppQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/register']}>
        <Routes>
          <Route path="/register" element={<RegisterPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetchMock;
}

describe('RegisterPage guest-work panel (AC-35)', () => {
  it('holds only the heading and the notice: no input, button, link or checked control', () => {
    renderRegister();

    const heading = screen.getByRole('heading', { name: REGISTER_PANEL_HEADING });
    const panel = heading.parentElement;
    expect(panel).not.toBeNull();
    if (panel === null) return;

    expect(within(panel).getByText(REGISTER_GUEST_WORK_NOTICE)).toBeInTheDocument();
    expect(panel.querySelector('input, button, a, select, textarea')).toBeNull();
    expect(within(panel).queryByRole('link')).toBeNull();
    expect(within(panel).queryByRole('button')).toBeNull();
    expect(within(panel).queryByRole('checkbox')).toBeNull();
    expect(panel.querySelector(':checked, [checked], [aria-checked="true"]')).toBeNull();
  });

  it('makes no request between rendering /register and submitting', async () => {
    const fetchMock = renderRegister();
    // Let effects, microtasks and any query that starts a tick later run before counting.
    await act(async () => {});
    await act(async () => {});

    expect(fetchMock).not.toHaveBeenCalled();
  });
});
