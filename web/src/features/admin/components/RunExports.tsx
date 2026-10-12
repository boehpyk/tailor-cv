export interface RunExportsProps {
  readonly actorId: string;
  readonly userId: string;
  readonly runId: string;
}

/**
 * A run's export jobs (AC-28): one row per job — document, format, layout, status — and
 * *Download* for a ready one (*older version* when `!current`); a non-ready job has no button.
 *
 * SKELETON (T11): a distinguishable placeholder.
 */
export function RunExports({ actorId, userId, runId }: RunExportsProps): React.JSX.Element {
  return <div data-actor={actorId} data-user={userId} data-run={runId} />;
}
