/**
 * Every sentence of the `/welcome` page (AC-28 – AC-31, copy C-10). Imported only by the marketing
 * chunk, never by a main-bundle module, so none of it ships to a visitor who never opens the page
 * (AC-32).
 *
 * The mockups' lines are kept **except four that would state something false** (C-10): TXT is
 * accepted too; nothing is "kept ready for every application" (a guest's CV is deleted after a day);
 * the posting is not "pulled apart" into requirements on screen; a guest's work is deleted after
 * 24 hours, not "after a while".
 *
 * **Slice 5.2 (AC-38): it sells what exists.** A guest gets a free fit score, not a tailoring; an
 * account's first tailoring is free; packs are shown as *opening soon* and nothing is sold. The
 * *Pro* plan and *Free to try, no account needed* are gone. No *token*, no promised gain (AC-37).
 *
 * The illustration's people and companies are **fictional** (AC-7, pinned to this folder by
 * `design.grep.test.ts`): an example, never a record.
 */

// --- Header ---------------------------------------------------------------------------------------

export const WORDMARK = 'FixCV';
export const NAV_HOW_IT_WORKS = 'How it works';
export const NAV_PRICING = 'Pricing';
export const NAV_LOG_IN = 'Log in';
export const NAV_TAILOR = 'Check my CV';

// --- Hero (1a, 1c) --------------------------------------------------------------------------------

export const HERO_BADGE = 'Free fit score, no account needed';
/** The `h1`: the lead, then the highlighted word and its full stop — one sentence as its name. */
export const HERO_HEADLINE_LEAD = 'Every job deserves its own';
export const HERO_HEADLINE_MARK = 'CV';
export const HERO_SUBLINE =
  'See how well your CV fits a job posting, free. Create an account and your first tailored CV and cover letter is free too.';
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
    title: 'Add the job and check your fit',
    body: 'Paste the posting or drop in a link, and get a fit score for free.',
  },
  {
    number: '03',
    title: 'Tailor, polish and download',
    body: 'With an account: tailor both documents, edit them in place, pick a layout and export.',
  },
] as const;

// --- Pricing (1d, C-10 corrections 3 and 4) --------------------------------------------------------

export const PRICING_HEADING = 'Start free. Stay if it helps.';
export const PRICING_SUBLINE =
  'Check your fit as a guest today. Make an account for your first tailoring, free, and to keep your work.';

export const PLAN_GUEST = {
  name: 'Guest',
  price: '$0',
  tagline: 'To see how well your CV fits a job.',
  features: [
    'A fit score for your CV and a job posting',
    "What the job asks for that your CV doesn't show",
    'No account needed',
  ],
  caveat: 'Work is deleted after 24 hours',
  cta: 'Check my CV',
} as const;

export const PLAN_ACCOUNT = {
  name: 'Account',
  badge: 'Most people',
  price: '$0',
  tagline: 'For a whole job search.',
  features: [
    'Your first tailored CV and cover letter, free',
    'Full editor and all four download formats',
    'Saved base CVs',
    'History of every tailoring',
    'Application board',
  ],
  cta: 'Create free account',
} as const;

/** OQ-1: shown, not sold. The card links to `/register`; nothing on this page takes a payment. */
export const PLAN_PACKS = {
  name: 'Packs',
  badge: 'Opening soon',
  price: 'From $5',
  tagline: 'For applying often.',
  features: [
    '3 credits for $5',
    '10 credits for $12',
    'One credit is one tailored CV and cover letter',
  ],
  note: 'Payments are opening soon. Nothing is charged.',
  cta: 'Start with a free account',
} as const;
