import type { TailoringRunSummary } from '@/features/tailoring/types';

export interface LatestRunCardProps {
  /**
   * The session's newest run from the list (`latestTailoringRun(items)`), or `null` for a session
   * with none. The summary is enough: the card links to `/runs/{id}` and never shows a document.
   */
  readonly run: TailoringRunSummary | null;
}

/**
 * The latest run, as a way back to it — presentational (AC-25). *In progress → View* while the
 * run is active, *Open your tailored documents* once it has succeeded.
 *
 * F5 skeleton: an empty section. The sentences and the links arrive in F7 against qa's red tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: read in F7
export function LatestRunCard(props: LatestRunCardProps): React.JSX.Element {
  return <section />;
}
