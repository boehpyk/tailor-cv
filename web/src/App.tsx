import { BaseCvUploadPanel } from './features/intake/components/BaseCvUploadPanel';
import { JobPostingPanel } from './features/posting/components/JobPostingPanel';
import { SystemStatus } from './features/health/components/SystemStatus';

/**
 * The application shell.
 *
 * Two product surfaces so far, both rendered as sections rather than behind routes: slice 1.1's
 * base-CV upload, and slice 1.2's job-posting intake below it. React Router is not installed until
 * 1.4, and the dual-tab workspace (CV | job posting) is that slice's design work — doing it badly
 * here means doing it twice.
 *
 * The order is the order of the task: you tailor a CV *to* a posting, so the CV comes first and the
 * posting second, and 1.3's "tailor" action will sit below both because it needs them both.
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

      <section aria-labelledby="job-posting-heading" className="mb-10">
        <h2 id="job-posting-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
          The job you are applying for
        </h2>
        <JobPostingPanel />
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
