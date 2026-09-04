import { SystemStatus } from './features/health/components/SystemStatus';

/**
 * The application shell.
 *
 * Phase 0 renders one thing: the dependency report. The dual-tab workspace (upload a CV / paste a
 * posting) arrives with slice 1.4, and deliberately not before — a shell full of buttons that do
 * nothing is harder to reason about than an honest empty one.
 */
export function App(): React.JSX.Element {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <header className="mb-10">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">TailorCraft</h1>
        <p className="mt-2 text-slate-600">
          Tailor your CV and cover letter to a job posting in under two minutes.
        </p>
      </header>

      <section aria-labelledby="system-status-heading">
        <h2
          id="system-status-heading"
          className="mb-3 text-sm font-medium text-slate-500 uppercase"
        >
          System status
        </h2>
        <SystemStatus />
      </section>
    </main>
  );
}
