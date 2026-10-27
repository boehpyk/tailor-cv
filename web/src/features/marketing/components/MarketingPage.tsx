/**
 * `/welcome` — the marketing page (web-design-refresh AC-28 – AC-31). **SKELETON (T33):** an empty
 * `main`, so `qa`'s RED tests run against the real lazy route and fail on their assertions.
 *
 * The default export is what `lazy()` needs. This module is reached **only** through the dynamic
 * import in `router.tsx`, which keeps it — and everything it imports — out of the main bundle.
 */
export default function MarketingPage(): React.JSX.Element {
  return <main />;
}
