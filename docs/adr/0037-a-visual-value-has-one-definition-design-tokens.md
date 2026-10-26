# ADR-0037: A visual value has one definition: design tokens in `@theme`, utilities only

- **Status:** Accepted
- **Date:** 2026-10-26
- **Relates to:** ADR-0036 (fonts — self-hosted; the font families are tokens here). Supersedes
  nothing.

## Context

Until the `web-design-refresh` slice, `index.css`'s `@theme` defined four brand shades and the app
otherwise used Tailwind's default palette by eye. That was fine for a utilitarian UI. The redesign
brings warm surfaces (`#fbfaf7`, `#f4f3ef`, `#e9e8e3`), a highlighter yellow, success and danger
ramps, larger radii and three font families — and mockups whose HTML writes those values inline,
dozens of times, as hex strings and `style=` attributes.

Copied as-is, the same colour would live in forty components. Changing it would be a search for a
string; contrast would be decided per component by whoever wrote it; and a reviewer could not tell a
deliberate new colour from a typo.

## Decision

1. **Colours, font families and radii are `@theme` tokens in `web/src/index.css`** — the only place
   one is written down. Tailwind v4 turns each into utilities (`bg-surface`, `text-success-700`,
   `font-display`, `rounded-xl`). Overriding Tailwind's own `--radius-*` names is deliberate: one
   definition restyles every `rounded-*` in the app, admin pages included.
2. **Components use utilities only.** No `style=` attribute, no colour literal (`#…`, `oklch(`,
   `rgb(`) and no arbitrary colour class (`bg-[#…]`) in production `.tsx`. A non-colour arbitrary
   *layout* value (`pb-[env(safe-area-inset-bottom)]`, a grid template) is allowed — it is geometry,
   not a visual identity.
3. **Text colour is chosen from a table, not by eye** (WCAG 2.2 AA, 4.5:1 for body text, computed
   from the tokens):

   | Use | Token | Allowed on |
   |---|---|---|
   | Body text | slate-900 / 700 / 600 | `surface`, `surface-sunk`, `page-grey`, white |
   | Secondary text | slate-600; slate-500 | slate-500 only on `surface` or white |
   | Links | brand-700 | `surface`, white |
   | Primary button label | white | brand-600 |
   | Text on highlighter | slate-900 | `highlight-100/200` |
   | **Never text** | slate-400, brand-500; slate-500 on `surface-sunk`/`page-grey` | — |

   The dashed `stitch` divider is decorative and carries no meaning.
4. **Enforced by `web/src/design.grep.test.ts`**, beside `security.grep.test.ts` and using its
   walker: the forbidden needles above, plus the marketing page's fictional illustration names
   (which may appear only under `features/marketing/`).

## Alternatives

- **Inline values as the mockups have them.** Fastest to transcribe, impossible to maintain, and
  contrast becomes a per-component accident. Rejected.
- **CSS modules / a CSS-in-JS theme object.** A second styling system beside Tailwind; tokens in a JS
  object are invisible to Tailwind's utilities. Rejected.
- **Tokens, but enforced by review only.** A rule nobody machine-checks erodes the first time a
  deadline meets a hex string. Rejected: the grep is twenty lines.
- **Semantic token names only (`--color-text-muted`).** Attractive, but it hides the contrast fact
  (which slate is it, on which surface?) behind a name, and the table above already says which use
  goes with which shade. Revisit if a dark mode arrives — that is when semantic aliases earn their
  keep.

## Consequences

- A new colour is an `index.css` diff a reviewer sees, not a string buried in a component.
- Contrast is a property of the table, checked once (`/verify` records the computed ratios), instead
  of re-decided in every component.
- The radius override restyles screens no task touched — intended, and checked visually when the
  tokens land.
- Dark mode, if it ever comes, is a token swap plus semantic aliases, not a component sweep.
