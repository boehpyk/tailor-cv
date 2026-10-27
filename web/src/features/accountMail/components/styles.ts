/**
 * The Tailwind classes every auth screen shares — the four account-mail screens and 2.1's `/login`
 * and `/register` — named once so they are one card (web-design-refresh AC-34) rather than six
 * that drift apart. Presentation only — no behaviour lives here. Text colours follow the AC-37
 * table: slate-900/700/600 for text, brand-700 for links, white on brand-600 for the primary
 * button, danger-600 for a refusal.
 */
const focusRing =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';

export const cardClass = 'rounded-2xl border border-slate-200 bg-white p-6 sm:p-8';
export const sectionClass = `mb-10 max-w-md ${cardClass}`;
export const headingClass =
  'mb-6 font-display text-2xl font-bold tracking-tight text-slate-900 focus:outline-none';
export const inputClass =
  'mt-1.5 block min-h-11 w-full rounded-xl border border-slate-300 bg-white px-3.5 text-base ' +
  `text-slate-900 ${focusRing} disabled:bg-slate-100 disabled:text-slate-600`;
export const labelClass = 'block text-sm font-semibold text-slate-700';
export const primaryButtonClass =
  'min-h-11 rounded-md bg-brand-600 px-5 text-sm font-semibold text-white hover:bg-brand-700 ' +
  `disabled:opacity-60 ${focusRing} motion-safe:transition-colors`;
export const secondaryButtonClass =
  'min-h-11 rounded-md border border-slate-300 bg-white px-5 text-sm font-semibold text-slate-900 ' +
  `hover:border-slate-400 disabled:opacity-60 ${focusRing}`;
export const linkClass = `rounded-sm font-medium text-brand-700 underline underline-offset-2 ${focusRing}`;
export const statusClass =
  'rounded-xl border border-success-500/30 bg-success-50 px-3.5 py-2.5 text-sm text-success-700';
export const alertClass =
  'rounded-xl border border-danger-200 bg-danger-50 px-3.5 py-2.5 text-sm text-danger-600';
