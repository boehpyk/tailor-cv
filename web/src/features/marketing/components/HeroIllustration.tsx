import { EXAMPLE_CV, EXAMPLE_DONE, EXAMPLE_POSTING, ILLUSTRATION_SUMMARY } from '../marketingCopy';

interface Run {
  readonly text: string;
  readonly mark?: boolean;
}

function Highlighted({ runs }: { readonly runs: readonly Run[] }) {
  return runs.map((run) =>
    run.mark === true ? (
      <span key={run.text} className="rounded-sm bg-highlight-100 px-0.5 text-slate-900">
        {run.text}
      </span>
    ) : (
      run.text
    ),
  );
}

/**
 * The hero's picture: a posting and the CV tailored to it (1a; 1c below `md`, where only the CV
 * card shows). **Decoration, not content** — the excerpts are fictional, so the cards are
 * `aria-hidden` and one visually-hidden sentence says what the picture is. The rotation is a
 * static transform; nothing here moves.
 */
export function HeroIllustration() {
  return (
    <figure className="relative mx-auto w-full max-w-sm md:h-[38rem] md:max-w-none">
      <figcaption className="sr-only">{ILLUSTRATION_SUMMARY}</figcaption>
      <div aria-hidden="true">
        <div className="absolute bottom-6 left-0 z-10 hidden w-64 -rotate-3 rounded-xl border border-slate-200 bg-white p-5 shadow-xl shadow-slate-900/10 md:block">
          <p className="text-xs font-semibold tracking-wide text-slate-600 uppercase">
            {EXAMPLE_POSTING.label}
          </p>
          <p className="mt-2.5 mb-1 font-display text-lg leading-tight font-bold">
            {EXAMPLE_POSTING.title}
          </p>
          <p className="text-sm text-slate-600">{EXAMPLE_POSTING.company}</p>
          <div className="stitch my-4" />
          <p className="text-sm leading-relaxed text-slate-700">
            <Highlighted runs={EXAMPLE_POSTING.body} />
          </p>
        </div>

        <div className="relative rounded-xl border border-slate-200 bg-white p-6 shadow-2xl shadow-slate-900/15 md:absolute md:top-0 md:right-0 md:w-84 md:rotate-1">
          <div className="flex items-center justify-between gap-3">
            <p className="font-display text-xl font-bold">{EXAMPLE_CV.name}</p>
            <span className="rounded-full bg-success-50 px-2.5 py-1 text-xs font-semibold text-success-700">
              {EXAMPLE_CV.badge}
            </span>
          </div>
          <p className="mt-0.5 text-sm text-slate-600">{EXAMPLE_CV.role}</p>
          <p className="mt-5 mb-2 text-xs font-semibold tracking-wide text-slate-600 uppercase">
            {EXAMPLE_CV.section}
          </p>
          <p className="text-sm font-semibold">{EXAMPLE_CV.position}</p>
          <ul className="mt-2 list-disc space-y-1 pl-4 text-sm leading-relaxed text-slate-700">
            {EXAMPLE_CV.bullets.map((runs) => (
              <li key={runs[1].text}>
                <Highlighted runs={runs} />
              </li>
            ))}
          </ul>
          <div className="stitch mt-4 mb-3.5" />
          <div className="flex gap-2 text-sm text-slate-600">
            <span className="rounded-md bg-slate-100 px-2.5 py-1.5 font-semibold text-slate-900">
              {EXAMPLE_CV.tabs[0]}
            </span>
            <span className="px-2.5 py-1.5">{EXAMPLE_CV.tabs[1]}</span>
          </div>
        </div>

        <p className="absolute -bottom-5 right-4 flex items-center gap-2.5 rounded-md bg-slate-900 px-4 py-3 text-sm text-white md:right-0 md:bottom-0">
          <span className="text-success-500">✓</span>
          {EXAMPLE_DONE}
        </p>
      </div>
    </figure>
  );
}
