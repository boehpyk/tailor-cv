import { useState } from 'react';

import { useCreateJobPosting } from '../hooks/useCreateJobPosting';
import { useJobPostings } from '../hooks/useJobPostings';

/**
 * The job-posting surface — a **container**, wired to `useJobPostings` (the list) and
 * `useCreateJobPosting` (the mutation).
 *
 * **SKELETON (T35). These are stubs and they must stay stubs until T36's tests are red.**
 *
 * Slice 1.1's equivalent step shipped a working component, and the RED that followed passed on
 * arrival — the cycle certified nothing, and the task list calls that out by name as the
 * degeneration this step exists to prevent. So what is here is: the right roles, placeholder text,
 * and the hooks actually wired. What is deliberately NOT here: the retention sentence, the two
 * input modes, the disabled-while-pending logic, the three error kinds, and any branching on
 * `code`. Every one of those is a thing T36 asserts, so every one of them has to be absent now or
 * the assertion proves nothing.
 */
export function JobPostingPanel(): React.JSX.Element {
  const { data, isPending, isError } = useJobPostings();
  const create = useCreateJobPosting();
  // Stub state: the mode exists so the shell compiles against the same shape T37 will use.
  const [mode] = useState<'pasted' | 'fetched'>('pasted');

  if (isPending) {
    return <p role="status">loading</p>;
  }

  if (isError) {
    return <p role="alert">list error</p>;
  }

  return (
    <div>
      <fieldset>
        <legend>stub input</legend>
        <p>mode: {mode}</p>
        <textarea aria-label="stub textarea" />
        <button type="button" disabled={create.isPending}>
          stub submit
        </button>
      </fieldset>
      {data.items.length === 0 && <p>stub empty</p>}
      {data.items.length > 0 && <p>stub card</p>}
      {create.isError && <p role="alert">stub error</p>}
    </div>
  );
}
