export interface ScoreComparisonPanelProps {
  /** A succeeded account run (the caller renders this only for `succeeded`). */
  readonly runId: string;
}

/**
 * Before and after tailoring (AC-18, AC-37), on an account's run page: idle (*Compare scores* and
 * the Gemini notice), per side scoring / stored / failed (+ *Try again*) / busy, unavailable, not
 * comparable, and the server's verdict. Reads `useRunFitScores(runId)`; writes through
 * `useCompareScores(runId)`, on a click only.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function ScoreComparisonPanel(_props: ScoreComparisonPanelProps): React.JSX.Element {
  return <div data-skeleton="ScoreComparisonPanel" />;
}
