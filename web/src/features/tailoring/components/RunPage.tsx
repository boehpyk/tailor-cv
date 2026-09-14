/**
 * The `/runs/:runId/:document` route — a **container**, once F7 gives it its query. It will read
 * `:runId` and `:document`, poll the run with `useTailoringRun` (1.3's poller, unchanged), derive
 * the view with `runView.ts` and render the stepper with stage 3 live; on `succeeded`, the editor
 * (AC-26).
 *
 * F5 skeleton: one `role="status"` region and nothing else — no params, no query, no copy. The four
 * states and the three error kinds arrive in F7 against qa's red tests.
 */
export function RunPage(): React.JSX.Element {
  return <div role="status" />;
}
