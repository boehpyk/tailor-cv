import { BaseCvUploadPanel } from '@/features/intake/components/BaseCvUploadPanel';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { TailorPanel } from '@/features/tailoring/components/TailorPanel';

/**
 * **Interim.** The `/` route's element between F3 (routes exist) and F5/F7 (`WorkspacePage` exists).
 *
 * This is exactly what `App` rendered before it became the layout route: slice 1.1's base-CV upload,
 * 1.2's job-posting intake and 1.3's tailoring panel, in the order of the task. It moved here
 * unchanged so that installing the router changes nothing a user can see at `/`. F5 adds
 * `WorkspacePage` beside this file and repoints the route; F11 deletes this file together with
 * `TailorPanel`. Nothing should be built on it.
 */
export function InterimWorkspace(): React.JSX.Element {
  return (
    <>
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
    </>
  );
}
