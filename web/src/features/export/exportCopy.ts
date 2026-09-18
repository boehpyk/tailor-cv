/**
 * The words the export bar says, kept out of the components that say them.
 *
 * A string a test pins (AC-35) and a string a component happens to contain are different things,
 * and the difference shows up when someone reflows the JSX. `features/tailoring/failureCopy.ts` made
 * the same call for the same reason: copy that a specification states verbatim lives in a module
 * whose whole content is that copy.
 *
 * This file grows in F6 with the `failure_reason` → sentence map — nine sentences, exhaustive over
 * `ExportFailureReason`, so a tenth reason on the server is a TypeScript error here rather than a
 * blank notice in the browser. It is deliberately not here yet: it is derivation input for
 * `viewOfExport`, and F5's tests are written before either exists.
 */

import type { ExportFormat } from './types';

/**
 * What each format is called on its control — and note that none of them is the wire spelling.
 *
 * *Word* rather than *DOCX*, because a job seeker in a hurry knows what Word is and may not know
 * what a DOCX is; *Plain text* rather than *TXT* for the same reason. *Markdown* and *PDF* are
 * already the names people use. The `Record<ExportFormat, string>` is exhaustive by type, so a fifth
 * format cannot ship with an unlabelled button.
 */
export const EXPORT_FORMAT_LABELS: Record<ExportFormat, string> = {
  md: 'Markdown',
  txt: 'Plain text',
  pdf: 'PDF',
  docx: 'Word',
};

/**
 * AC-35, verbatim — where the file is made, how long it lives, and who else sees it.
 *
 * **This sentence is the export bar's half of the privacy promise** (Constitution §8, ADR-0006),
 * and it is on the bar rather than in a policy page nobody opens, exactly as 1.3 put the Gemini
 * disclosure next to the button that sends the CV. Each clause is a fact about this system and not
 * a reassurance: the render happens in our worker (no third party renders the PDF), the file is
 * deleted with the guest session inside 24 hours (a scheduled job enforces it), and nothing about
 * the document leaves this server in the process.
 *
 * Exported as a constant because a Vitest assertion pins it. Change the wording here and the test
 * fails, which is the intended cost — a sentence that promises a retention window should not be
 * edited by accident.
 */
export const EXPORT_PRIVACY_NOTE =
  'Your files are made on our server, kept for 24 hours, and never sent anywhere else.';
