import { useId } from 'react';
import { Link } from 'react-router';

import logo from '@/assets/brand/fixcv-logo-light.png';

import {
  HERO_BADGE,
  HERO_DOWNLOAD_AS,
  HERO_EXAMPLE,
  HERO_FORMATS,
  HERO_HEADLINE_LEAD,
  HERO_HEADLINE_MARK,
  HERO_SUBLINE,
  HERO_UPLOAD,
  NAV_HOW_IT_WORKS,
  NAV_LOG_IN,
  NAV_PRICING,
  NAV_TAILOR,
  PLAN_ACCOUNT,
  PLAN_GUEST,
  PLAN_PRO,
  PRICING_HEADING,
  PRICING_SUBLINE,
  STEPS,
  STEPS_HEADING,
  WORDMARK,
} from '../marketingCopy';

import { HeroIllustration } from './HeroIllustration';

const focusRing =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';
const primaryButton = `inline-flex min-h-11 items-center justify-center rounded-md bg-brand-600 px-6 font-semibold text-white hover:bg-brand-700 motion-safe:transition-colors ${focusRing}`;
const secondaryButton = `inline-flex min-h-11 items-center justify-center rounded-md border border-slate-300 bg-white px-5 font-semibold text-slate-900 hover:border-slate-400 motion-safe:transition-colors ${focusRing}`;
const navLink = `rounded-sm text-slate-700 hover:text-slate-900 ${focusRing}`;
/** The one nav link still shown below `sm` gets a 44 × 44 px target there (AC-38). */
const navLinkTouch =
  'max-sm:inline-flex max-sm:min-h-11 max-sm:min-w-11 max-sm:items-center max-sm:justify-center';

function Features({ items, tone }: { readonly items: readonly string[]; readonly tone: string }) {
  return (
    <ul className={`space-y-2.5 text-[15px] ${tone}`}>
      {items.map((item) => (
        <li key={item} className="flex gap-2">
          <span aria-hidden="true">✓</span>
          {item}
        </li>
      ))}
    </ul>
  );
}

/**
 * `/welcome` — the marketing page (web-design-refresh AC-28 – AC-33, copy C-10).
 *
 * **Static, on purpose.** It lives outside the app shell (OQ-4) and imports no hook, no `api/*`, no
 * `useAuth`: it cannot know whether the visitor is signed in without a request, so it does not ask.
 * A signed-in visitor who clicks *Log in* is forwarded by `/login`'s own guard. Nothing here writes
 * a cookie or storage — a visitor reading about the product has not started a guest session.
 *
 * The default export is what `lazy()` needs. This module is reached **only** through the dynamic
 * import in `router.tsx`, which keeps it — and everything it imports — out of the main bundle
 * (AC-32). One DOM for every width: below `md` the hero stacks and the pricing cards stack (1c;
 * 1f's plan switch is not built, OQ-19).
 */
export default function MarketingPage(): React.JSX.Element {
  const proNoteId = useId();

  return (
    <div className="min-h-dvh bg-surface">
      <header>
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-8 gap-y-2 px-4 py-4">
          <Link to="/" className={`-my-2 flex rounded-sm ${focusRing}`}>
            <img src={logo} alt={WORDMARK} width={384} height={150} className="h-16 w-auto" />
          </Link>
          <nav className="flex items-center gap-6 text-[15px] sm:gap-8">
            <a href="#how-it-works" className={`max-sm:hidden ${navLink}`}>
              {NAV_HOW_IT_WORKS}
            </a>
            <a href="#pricing" className={`max-sm:hidden ${navLink}`}>
              {NAV_PRICING}
            </a>
            <Link to="/login" className={`${navLinkTouch} ${navLink}`}>
              {NAV_LOG_IN}
            </Link>
            <Link
              to="/"
              className={`inline-flex min-h-11 items-center rounded-sm bg-slate-900 px-4 font-semibold text-white hover:bg-slate-700 motion-safe:transition-colors ${focusRing}`}
            >
              {NAV_TAILOR}
            </Link>
          </nav>
        </div>
      </header>

      <main>
        <section className="mx-auto grid max-w-6xl items-center gap-14 px-4 pt-10 pb-16 md:grid-cols-2 md:pt-16 md:pb-20">
          <div className="flex flex-col items-start gap-6">
            <p className="rounded-full bg-brand-50 px-3.5 py-1.5 text-sm font-semibold text-brand-700">
              {HERO_BADGE}
            </p>
            <h1 className="font-display text-5xl leading-none font-bold tracking-tight text-balance text-slate-900 md:text-7xl">
              {HERO_HEADLINE_LEAD}{' '}
              {/* The full stop sits inside the mark: an element boundary between "CV" and "." makes
                  some accessible-name engines (jsdom's among them) read "CV ." */}
              <span className="bg-linear-to-b from-transparent from-60% to-highlight-200 to-60%">
                {HERO_HEADLINE_MARK}.
              </span>
            </h1>
            <p className="max-w-md text-lg leading-normal text-slate-600 md:text-xl">
              {HERO_SUBLINE}
            </p>
            <div className="flex flex-wrap gap-3 max-sm:w-full">
              <Link to="/" className={`${primaryButton} max-sm:w-full`}>
                {HERO_UPLOAD}
              </Link>
              <a href="#how-it-works" className={`${secondaryButton} max-sm:hidden`}>
                {HERO_EXAMPLE}
              </a>
            </div>
            <div className="flex flex-wrap items-center gap-2.5 text-sm text-slate-600">
              <span>{HERO_DOWNLOAD_AS}</span>
              {HERO_FORMATS.map((format) => (
                <span
                  key={format}
                  className="rounded-md border border-slate-200 bg-white px-2 py-0.5 font-mono text-xs font-medium text-slate-700"
                >
                  {format}
                </span>
              ))}
            </div>
          </div>
          <HeroIllustration />
        </section>

        <section
          id="how-it-works"
          aria-labelledby="how-it-works-heading"
          className="mx-auto max-w-6xl scroll-mt-4 px-4"
        >
          <h2 id="how-it-works-heading" className="sr-only">
            {STEPS_HEADING}
          </h2>
          <ol className="stitch grid md:grid-cols-3">
            {STEPS.map((step, index) => (
              <li
                key={step.number}
                className={`py-6 md:pt-8 md:pb-12 ${
                  index === 0
                    ? 'md:pr-8'
                    : 'border-dashed border-slate-300 max-md:border-t-[1.5px] md:border-l-[1.5px] md:px-8'
                }`}
              >
                <p className="font-mono text-sm font-medium text-brand-700">{step.number}</p>
                <h3 className="my-2 font-display text-2xl font-bold text-slate-900">
                  {step.title}
                </h3>
                <p className="text-[15px] leading-relaxed text-slate-600">{step.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <section
          id="pricing"
          aria-labelledby="pricing-heading"
          className="mx-auto max-w-6xl scroll-mt-4 px-4 pt-16 pb-20 md:pt-18"
        >
          <div className="mb-12 flex flex-col items-center gap-3.5 text-center">
            <h2
              id="pricing-heading"
              className="font-display text-4xl leading-tight font-bold tracking-tight text-balance text-slate-900 md:text-6xl"
            >
              {PRICING_HEADING}
            </h2>
            <p className="text-lg text-slate-600">{PRICING_SUBLINE}</p>
          </div>

          <div className="grid gap-5 md:grid-cols-3">
            <article className="flex flex-col gap-4.5 rounded-2xl border border-slate-200 bg-white p-8">
              <h3 className="font-display text-2xl font-bold text-slate-900">{PLAN_GUEST.name}</h3>
              <p className="font-display text-5xl font-bold text-slate-900">{PLAN_GUEST.price}</p>
              <p className="text-[15px] leading-normal text-slate-600">{PLAN_GUEST.tagline}</p>
              <div className="stitch" />
              <Features items={PLAN_GUEST.features} tone="text-slate-700" />
              <p className="text-[15px] text-slate-600">{PLAN_GUEST.caveat}</p>
              <Link to="/" className={`${secondaryButton} mt-auto`}>
                {PLAN_GUEST.cta}
              </Link>
            </article>

            <article className="flex flex-col gap-4.5 rounded-2xl bg-slate-900 p-8 text-white">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-display text-2xl font-bold">{PLAN_ACCOUNT.name}</h3>
                <span className="rounded-full bg-highlight-200 px-2.5 py-1 text-xs font-semibold text-slate-900">
                  {PLAN_ACCOUNT.badge}
                </span>
              </div>
              <p className="font-display text-5xl font-bold">{PLAN_ACCOUNT.price}</p>
              <p className="text-[15px] leading-normal text-slate-300">{PLAN_ACCOUNT.tagline}</p>
              <div className="border-t-[1.5px] border-dashed border-slate-600" />
              <Features items={PLAN_ACCOUNT.features} tone="text-slate-200" />
              <Link
                to="/register"
                className="mt-auto inline-flex min-h-11 items-center justify-center rounded-md bg-brand-600 px-6 font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white motion-safe:transition-colors"
              >
                {PLAN_ACCOUNT.cta}
              </Link>
            </article>

            <article className="flex flex-col gap-4.5 rounded-2xl border border-slate-200 bg-white p-8">
              <h3 className="font-display text-2xl font-bold text-slate-900">{PLAN_PRO.name}</h3>
              <p className="text-slate-900">
                <span className="font-display text-5xl font-bold">{PLAN_PRO.price}</span>{' '}
                <span className="text-slate-600">{PLAN_PRO.period}</span>
              </p>
              <p className="text-[15px] leading-normal text-slate-600">{PLAN_PRO.tagline}</p>
              <div className="stitch" />
              <Features items={PLAN_PRO.features} tone="text-slate-700" />
              <div className="mt-auto space-y-2">
                <button
                  type="button"
                  disabled
                  aria-describedby={proNoteId}
                  className="min-h-11 w-full rounded-md border border-slate-300 font-semibold text-slate-600 disabled:cursor-not-allowed"
                >
                  {PLAN_PRO.cta}
                </button>
                <p id={proNoteId} className="text-center text-sm text-slate-600">
                  {PLAN_PRO.note}
                </p>
              </div>
            </article>
          </div>
        </section>
      </main>
    </div>
  );
}
