export interface DemandPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key. */
  readonly actorId: string;
}

/**
 * `/admin/demand` (AC-29, AC-39, ADR-0043): *From* / *To* native date inputs (the range lives in
 * the URL), then loading, error, empty (*Nothing counted in this range.*) or the table of days with
 * a totals row. Reads `useDemandCounters(actorId, from, to)`.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function DemandPage(_props: DemandPageProps): React.JSX.Element {
  return <div data-skeleton="DemandPage" />;
}
