import { BaseCvUploadPanel } from './features/intake/components/BaseCvUploadPanel';
import { JobPostingPanel } from './features/posting/components/JobPostingPanel';
import { TailorPanel } from './features/tailoring/components/TailorPanel';
import { SystemStatus } from './features/health/components/SystemStatus';

/**
 * The application shell.
 *
 * Three product surfaces, each rendered as a section rather than behind a route: slice 1.1's
 * base-CV upload, slice 1.2's job-posting intake, and slice 1.3's tailoring panel. React Router is
 * not installed until 1.4, and the dual-tab workspace (CV | cover letter) is that slice's design
 * work — doing it badly here means doing it twice.
 *
 * The order is the order of the task. You tailor a CV *to* a posting, so the CV comes first and the
 * posting second, and tailoring sits below both because it needs them both. System status is
 * diagnostic rather than part of the task, so it stays last.
 *
 * **The shell passes no props.** Each panel reads the server state it needs from TanStack Query,
 * using the same query keys as its siblings. `TailorPanel` gets the CV and posting from the cache
 * that `BaseCvUploadPanel` and `JobPostingPanel` fill, so there is still one request per list. It
 * does not get them threaded through `App`, which would turn this shell into a relay for data it
 * never uses. Each `<section aria-labelledby>` is the landmark, which is why the panels render no
 * heading of their own.
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

      <section aria-labelledby="tailoring-heading" className="mb-10">
        <h2 id="tailoring-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
          Your tailored CV and cover letter
        </h2>
        <TailorPanel />
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
