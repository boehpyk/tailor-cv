# ADR-0036: Fonts and every static asset are self-hosted; the built bundle names no third-party host

- **Status:** Accepted
- **Date:** 2026-10-26
- **Relates to:** Constitution §8 (privacy), ADR-0037 (design tokens — the fonts are tokens there).
  Supersedes nothing.

## Context

The `web-design-refresh` slice brings three web fonts into an app that had none (Bricolage Grotesque
for display, Figtree for UI, JetBrains Mono). The designer's mockups load them the usual way: a
`<link>` to `fonts.googleapis.com`, which pulls the files from `fonts.gstatic.com`.

That is a request to a third party on **every page load, from every visitor**, carrying their IP
address, user agent and the page's origin in `Referer`/`Origin`. For a product whose users hand over
a CV — often while unemployed and in a hurry — it is a leak of who is using FixCV, to a party
the user never agreed to talk to, for a decoration. (A German court found exactly this embedding
unlawful in 2022; the legal point matters less than the principle: Constitution §8 says nothing is
sent anywhere the user was not told about.)

Nothing in the build stops it today. A `@import url(https://…)` in a stylesheet, a CDN `<script>` in
`index.html`, or a dependency that references an asset host would ship silently, and no test would
notice, because jsdom fetches nothing.

## Decision

1. **Fonts are bundled from `@fontsource/*` packages** and served from the app's own origin like any
   other Vite asset. Exact versions in `package.json` (no `^`), each published at least 14 days before
   install. **Latin subset CSS only** (`latin-<weight>.css`), only the weights in use (seven files),
   `font-display: swap` (shipped by `@fontsource`, verified on the installed version). Vite never
   inlines a `.woff2` (`assetsInlineLimit` excludes it), so a font is a cacheable file, not bytes
   repeated in every CSS download.
2. **`web/scripts/check-dist-hosts.mjs` runs at the end of `npm run build`** and fails the build if
   `dist/` (a) loads anything from a scheme-qualified host — a CSS `url(`/`@import`, an HTML
   `<link href>`/`<script src>` — or (b) contains any `http(s)://host` literal not on a short,
   printed allow-list of namespace and documentation URLs that are never fetched (the SVG/XHTML
   namespaces, React's error-decoder URL, and the like). `fonts.googleapis.com` and
   `fonts.gstatic.com` are refused by name. Because CI, `make web.build` and the production image
   all run `npm run build`, the guard runs in all three without a workflow, Makefile or Dockerfile
   edit.
3. **Any future third-party asset** — a CDN script, a hosted font, an analytics beacon — needs an
   ADR superseding this one, and an allow-list entry the reviewer sees in the diff.

## Alternatives

- **Google Fonts `<link>` as the mockups do.** The easy path and the reason for this ADR: a
  per-visitor request to a third party. Rejected.
- **`@fontsource-variable/*`** — fewer files and Bricolage's optical-size axis, but the packages emit
  every subset, so `dist` grows for glyphs nobody renders. Rejected for now (OQ-15); revisit if a
  weight is added.
- **Download the `.woff2` files by hand into `public/`.** No third party, but no version, no
  provenance and no upgrade path — a binary blob nobody can audit. Rejected: a pinned package is the
  same bytes with a name.
- **A Content-Security-Policy (`font-src 'self'`) instead of a build check.** A CSP enforces it in the
  browser, which is stronger, but it lives in nginx (devops), fails at runtime instead of at build,
  and the app has no CSP at all yet. Not instead — **after**: see Consequences.
- **A source grep instead of a `dist` scan.** Source does not see what a dependency or Vite emits;
  the built bundle is what users download. Rejected.

## Consequences

- No visitor's browser talks to anyone but `cv.samolit.com` for the app's own assets, and the build
  proves it on every run.
- The allow-list is a maintenance point: a library upgrade that adds a harmless URL literal turns
  the build red until someone reads it and adds it. That friction is the point.
- `swap` shows the system font first and reflows once when Figtree arrives (a small layout shift).
  Accepted; the follow-up is `size-adjust` on a fallback `@font-face`, never a CDN preconnect.
- **Follow-up (devops, not this slice):** a CSP in nginx starting from `default-src 'self'`, which
  turns this build-time promise into a browser-enforced one.
