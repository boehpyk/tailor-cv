/**
 * Every sentence of the `/welcome` page (AC-28 – AC-31, copy C-10). Imported only by the marketing
 * chunk, never by a main-bundle module, so none of it ships to a visitor who never opens the page
 * (AC-32).
 *
 * The mockups' lines are kept **except four that would state something false** (C-10): TXT is
 * accepted too; nothing is "kept ready for every application" (a guest's CV is deleted after a day);
 * the posting is not "pulled apart" into requirements on screen; a guest's work is deleted after
 * 24 hours, not "after a while". Pro is shown and not sold (OQ-3).
 *
 * The illustration's people and companies are **fictional** (AC-7, pinned to this folder by
 * `design.grep.test.ts`): an example, never a record.
 */

// --- Header ---------------------------------------------------------------------------------------

export const WORDMARK = 'FixCV';
export const NAV_HOW_IT_WORKS = 'How it works';
export const NAV_PRICING = 'Pricing';
export const NAV_LOG_IN = 'Log in';
export const NAV_TAILOR = 'Tailor my CV';

// --- Hero (1a, 1c) --------------------------------------------------------------------------------

export const HERO_BADGE = 'Free to try, no account needed';
/** The `h1`: the lead, then the highlighted word and its full stop — one sentence as its name. */
export const HERO_HEADLINE_LEAD = 'Every job deserves its own';
export const HERO_HEADLINE_MARK = 'CV';
export const HERO_SUBLINE =
  'Tailor your CV and cover letter to a job posting in under two minutes.';
export const HERO_UPLOAD = 'Upload my CV';
export const HERO_EXAMPLE = 'See an example';
export const HERO_DOWNLOAD_AS = 'Download as';
export const HERO_FORMATS = ['PDF', 'DOCX', 'MD', 'TXT'] as const;

// --- The illustration (decorative, aria-hidden; fictional) -----------------------------------------

export const ILLUSTRATION_SUMMARY = 'An example: a job posting and the CV tailored to it';

export const EXAMPLE_POSTING = {
  label: 'Job posting',
  title: 'Senior Product Designer',
  company: 'Northwind · Remote',
  /** Plain text and highlighted phrases, in reading order. */
  body: [
    { text: 'You’ll own ' },
    { text: 'design systems', mark: true },
    { text: ' end-to-end, run ' },
    { text: 'user research', mark: true },
    { text: ', and partner with engineering on ' },
    { text: 'accessibility', mark: true },
    { text: '.' },
  ],
} as const;

export const EXAMPLE_CV = {
  name: 'Maya Okafor',
  badge: 'Tailored',
  role: 'Product Designer',
  section: 'Experience',
  position: 'Lead Designer, Fieldnote',
  bullets: [
    [
      { text: 'Built and maintained the ' },
      { text: 'company-wide design system', mark: true },
      { text: ' used by 6 product teams' },
    ],
    [
      { text: 'Ran ' },
      { text: '40+ user research sessions', mark: true },
      { text: ' to shape the 2025 roadmap' },
    ],
    [
      { text: 'Partnered with engineers to reach ' },
      { text: 'WCAG AA', mark: true },
      { text: ' across the app' },
    ],
  ],
  tabs: ['CV', 'Cover letter'],
} as const;

export const EXAMPLE_DONE = 'Tailored in 1 min 12 s';

// --- How it works (1a, C-10 corrections 1 and 2) ---------------------------------------------------

export const STEPS_HEADING = 'How it works';
export const STEPS = [
  {
    number: '01',
    title: 'Upload your base CV',
    body: 'PDF, DOCX or TXT. We read it once.',
  },
  {
    number: '02',
    title: 'Add the job',
    body: 'Paste the posting or drop in a link.',
  },
  {
    number: '03',
    title: 'Polish and download',
    body: 'Edit both documents in place, pick a layout, and export when it feels right.',
  },
] as const;

// --- Pricing (1d, C-10 corrections 3 and 4) --------------------------------------------------------

export const PRICING_HEADING = 'Start free. Stay if it helps.';
export const PRICING_SUBLINE =
  'Try it as a guest today. Make an account when you want to keep your work.';

export const PLAN_GUEST = {
  name: 'Guest',
  price: '$0',
  tagline: 'For a quick application, right now.',
  features: ['Tailored CV and cover letter', 'Full editor', 'All four download formats'],
  caveat: 'Work is deleted after 24 hours',
  cta: 'Try without signing up',
} as const;

export const PLAN_ACCOUNT = {
  name: 'Account',
  badge: 'Most people',
  price: '$0',
  tagline: 'For a whole job search.',
  features: [
    'Everything in Guest',
    'Saved base CVs',
    'History of every tailoring',
    'Application board',
  ],
  cta: 'Create free account',
} as const;

export const PLAN_PRO = {
  name: 'Pro',
  price: '$9',
  period: '/ month',
  tagline: 'For applying often.',
  features: ['Everything in Account', 'More tailorings per day', 'Priority queue'],
  cta: 'Go Pro',
  /** OQ-3: there is no billing, so the button is disabled and says why. */
  note: 'Coming soon — nothing is charged.',
} as const;
