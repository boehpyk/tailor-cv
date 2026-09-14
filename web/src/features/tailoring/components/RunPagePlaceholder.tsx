/**
 * **Interim.** The `/runs/:runId/:document` element between F3 (routes exist) and F5 (`RunPage`
 * exists). One line, no data, no params. F5 adds `RunPage` beside this file and repoints the route;
 * this file is deleted with it. Nothing should be built on it.
 */
export function RunPagePlaceholder(): React.JSX.Element {
  return <p className="text-sm text-slate-500">This run page arrives in a later task.</p>;
}
