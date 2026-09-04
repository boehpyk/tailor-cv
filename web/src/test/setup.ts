import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// React Testing Library does not unmount between tests on its own under Vitest. Without this, a
// component from a previous test is still in the document and `getByText` finds two matches — which
// presents as a confusing "found multiple elements" failure in a test that is entirely correct.
afterEach(() => {
  cleanup();
});
