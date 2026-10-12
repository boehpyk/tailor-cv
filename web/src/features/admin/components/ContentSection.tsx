import { ErrorWithRetry } from './ContentPage';

import type { UseQueryResult } from '@tanstack/react-query';

export interface ContentSectionProps {
  /** Unique within the page: labels the `<section>` by its `<h2>`. */
  readonly id: string;
  readonly heading: string;
  readonly query: Pick<UseQueryResult, 'data' | 'error' | 'refetch' | 'isFetching'>;
  readonly loadingText: string;
  readonly errorText: string;
  /** Rendered once the first answer arrived: rows, or the empty sentence. */
  readonly children: React.ReactNode;
}

/**
 * One section of the account's Content area (AC-26): its `<h2>`, then exactly one of loading
 * (`role="status"`), error + Retry (`role="alert"`) or what `children` render. Data already shown
 * stays shown when a refetch fails.
 */
export function ContentSection({
  id,
  heading,
  query,
  loadingText,
  errorText,
  children,
}: ContentSectionProps): React.JSX.Element {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="text-base font-semibold text-slate-900">
        {heading}
      </h2>
      {query.data !== undefined ? (
        children
      ) : query.error !== null ? (
        <ErrorWithRetry text={errorText} query={query} />
      ) : (
        <p role="status" className="text-sm text-slate-600">
          {loadingText}
        </p>
      )}
    </section>
  );
}

export const ROW_LINK = 'font-medium text-slate-900 underline underline-offset-2 break-words';
export const EMPTY = 'text-sm text-slate-600';
