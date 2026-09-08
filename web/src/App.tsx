import { BaseCvUploadPanel } from './features/intake/components/BaseCvUploadPanel';
import { SystemStatus } from './features/health/components/SystemStatus';

/**
 * The application shell.
 *
 * Slice 1.1 (`intake-base-cv-upload`) adds the first real product surface: upload a base CV,
 * see it get extracted. It renders as a section here rather than behind a route, because React
 * Router is not installed until 1.4 (technical-plan.md) — the dual-tab workspace (CV / job
 * posting) is that slice's job, not this one's.
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

      <section aria-labelledby="base-cv-heading" className="mb-10">
        <h2 id="base-cv-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
          Your base CV
        </h2>
        <BaseCvUploadPanel />
      </section>

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
