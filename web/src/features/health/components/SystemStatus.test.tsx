import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithQuery } from '@/test/render';

import { SystemStatus } from './SystemStatus';

function respondWith(status: number, body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );
}

describe('SystemStatus', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows a loading state before the first response arrives', () => {
    respondWith(200, { ready: true, dependencies: {} });

    renderWithQuery(<SystemStatus />);

    expect(screen.getByText(/checking dependencies/i)).toBeInTheDocument();
  });

  it('lists every dependency the API reported', async () => {
    respondWith(200, {
      ready: true,
      dependencies: {
        postgres: { healthy: true, detail: null },
        redis: { healthy: true, detail: null },
        celery: { healthy: true, detail: '1 worker(s)' },
      },
    });

    renderWithQuery(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText('All dependencies healthy')).toBeInTheDocument();
    });
    expect(screen.getByText('postgres')).toBeInTheDocument();
    expect(screen.getByText('celery')).toBeInTheDocument();
  });

  it('renders the 503 report rather than treating it as a failed request', async () => {
    // The API answers 503 WITH the report naming what is down. Throwing there would discard the one
    // piece of information worth having and replace it with "request failed".
    respondWith(503, {
      ready: false,
      dependencies: {
        postgres: { healthy: true, detail: null },
        celery: { healthy: false, detail: 'no workers responded' },
      },
    });

    renderWithQuery(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText('Not ready')).toBeInTheDocument();
    });
    expect(screen.getByText('no workers responded')).toBeInTheDocument();
  });

  it('distinguishes an unreachable API from an unhealthy one', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderWithQuery(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText(/could not reach the api/i)).toBeInTheDocument();
    });
  });
});
