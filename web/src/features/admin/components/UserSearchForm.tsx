import { useId } from 'react';

import { SEARCH_LABEL, SEARCH_SUBMIT_LABEL } from '../adminCopy';

export interface UserSearchFormProps {
  /** The search in the URL; the uncontrolled input is keyed by it, so back/forward restore it. */
  readonly q: string;
  /** Called on submit with the typed text (trimmed by the page, never per keystroke). */
  readonly onSearch: (q: string) => void;
}

/**
 * `<form role="search">` with a labelled input and a *Search* button (AC-32). Presentational.
 */
export function UserSearchForm({ q, onSearch }: UserSearchFormProps): React.JSX.Element {
  const inputId = useId();
  return (
    <form
      role="search"
      onSubmit={(event) => {
        event.preventDefault();
        const typed = new FormData(event.currentTarget).get('q');
        onSearch(typeof typed === 'string' ? typed : '');
      }}
      className="flex gap-2"
    >
      <label htmlFor={inputId} className="sr-only">
        {SEARCH_LABEL}
      </label>
      <input
        key={q}
        id={inputId}
        name="q"
        type="search"
        defaultValue={q}
        className="rounded-md border border-slate-300 px-3 py-2 text-sm"
      />
      <button
        type="submit"
        className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
      >
        {SEARCH_SUBMIT_LABEL}
      </button>
    </form>
  );
}
