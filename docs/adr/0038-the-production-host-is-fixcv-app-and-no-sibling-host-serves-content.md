# ADR-0038: The production host is `fixcv.app`; `www` is a Traefik redirect; no sibling host under it ever serves content

- **Status:** Accepted
- **Date:** 2026-10-27
- **Relates to:** ADR-0003 (single VDS behind the shared Traefik, unchanged), ADR-0008 (a) and
  amendment (m), ADR-0010 (c), ADR-0021 §4 (the `Origin` check), ADR-0026 (the mail sender). The
  `samolit.com` text in those ADRs is historical. Supersedes nothing.

## Context

Production served `cv.samolit.com`. That host shared its registrable domain with every other
`*.samolit.com` app behind the same Traefik. A sibling host is *same-site*, and it can **set** a
cookie with `Domain=samolit.com`, which a browser then sends to us as well. `__Host-tc_guest`
(ADR-0010 (c)) is immune, because the prefix forbids `Domain`. `tc_refresh` is not: its
`Path=/api/auth` cannot take `__Host-`, which requires `Path=/`. That planting exposure is **OQ-16**,
a login-CSRF into an attacker's account. It was scheduled as its own slice.

The owner registered `fixcv.app` (2026-10-27) as the product's own domain. By then
`cv.samolit.com` had no DNS record, and the owner chose not to restore it, because no customers
depend on it.

The app learns its own origin from exactly one setting, `PUBLIC_BASE_URL` (mail links, the `Origin`
check, the posting fetcher's `User-Agent`). `CORS_ORIGINS` is the only other host-bearing value. So
the move is configuration, not code.

## Decision

1. **The production host is the apex `fixcv.app`.** The box `.env` carries
   `PUBLIC_BASE_URL=https://fixcv.app` and `CORS_ORIGINS=https://fixcv.app`. No other origin is
   trusted, and the old one deliberately is not.
2. **Any other name redirects to the apex at the proxy, never in nginx or the app.** Traefik
   routes `www.fixcv.app` through a `redirectregex` middleware (`tailorcraft-to-apex`, permanent)
   that keeps path and query. **One router per host**: Traefik requests one certificate for all of
   a router's `Host()`s, so a name failing validation would take the others' certificate with it.
   `cv.samolit.com` gets no router (it does not resolve).
3. **No host under `fixcv.app` other than the redirect ever serves content.** This is a standing
   rule, not a preference. It covers **DNS, not just our routers**: any record that points a
   `*.fixcv.app` name at an HTTP server (a hosted status or docs page, a mail provider's
   click-tracking CNAME, a CNAME left dangling) creates a sibling that can set a
   `Domain=fixcv.app` cookie. It is what makes the host-only `tc_refresh` safe without a `__Host-`
   prefix, because every name that could plant a `Domain=fixcv.app` cookie belongs to this app and
   serves nothing. **Reopen trigger:** before any other `*.fixcv.app` name is pointed at
   an HTTP server, ours or anyone's, OQ-16
   reopens and the `__Host-` rename of `tc_refresh` is scheduled first.
4. **The mail sender stays `notifications.samolit.com`** for now. A From domain that differs from
   the link domain is legitimate and already passes SPF/DKIM/DMARC. Moving it is its own step
   (Resend verification plus DNS records).

## Alternatives

- **Restore `cv.samolit.com` and redirect it.** This was the planner's recommendation, so that
  bookmarks and old mail links survive. The owner rejected it: there are no customers there, and a
  redirect also costs a router and a certificate.
- **Keep both hosts serving the app.** This would mean two origins in `CORS_ORIGINS` and the
  `Origin` check, two cookie jars per user, and OQ-16 kept alive on the old one. Trust for nothing.
- **Redirect in nginx (`server` block on the host).** nginx is `server_name _` and host-agnostic on
  purpose, and Traefik would still need the router for the certificate. More config, same result.
- **Rename `tc_refresh` to `__Host-` and move it to `Path=/`.** This fixes OQ-16 on any domain, but
  it widens where the refresh cookie travels and costs a slice. On an apex nobody else serves, the
  condition in Decision 3 buys the same protection for free, with an explicit trigger for when it
  stops being enough.

## Consequences

- **Everyone is signed out once.** Host-only cookies cannot follow a host move, which is the same
  property that made them safe. Guests lose access to their ≤ 24 h work. Its rows expire and go at
  the next purge, and stranded refresh families go by the hourly identity token sweep.
- **OQ-16 is closed by condition** (ADR-0008 (m)). The condition must be re-read before anyone adds
  any DNS record under `fixcv.app`.
- **Changing host again is a runbook, not a slice** (`docs/infrastructure.md`, *Changing the
  production host*): DNS, then merge, then edit the box `.env` while the deploy waits, then
  `check-settings` in a one-off container, then approve. A running container keeps the environment
  it was created with, which is what makes that ordering safe.
- The deploy's smoke step checks `https://fixcv.app/health/ready` and that `www` answers a 301 with
  path and query intact.
