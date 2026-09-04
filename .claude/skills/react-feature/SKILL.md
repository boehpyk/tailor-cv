---
name: react-feature
description: How to add a frontend feature in TailorCraft — where state lives (server state to TanStack Query, form state local, nothing global by default), custom-hook boundaries, the typed API client, the tabbed TipTap editor pattern, polling a background export, and the component-splitting rules. Use when building or reviewing anything under web/.
---

# Adding a frontend feature

Learning modern React architecture is a ranked goal (Constitution §2, US-7), so the bar here is not
"it works" — it is "someone reading this learns the right habit".

## The decision you make most often: where does this state live?

| Kind | Where | Why |
|---|---|---|
| Came from the API | **TanStack Query** | One cache, one source of truth, automatic invalidation, no manual sync |
| Being typed right now | Local `useState` / TipTap's document | It is transient and belongs to one owner |
| Derived from other state | **Computed during render** | A derived value in `useState` is a bug that goes stale |
| Which tab, which run | The URL (React Router) | Shareable, back-button-correct, free |
| Truly global client state | Context, and justify it | Rare. Auth is the real example. |

**If you are writing `useEffect` to fetch data, stop.** That is the single most common React mistake
in an app shaped like this one, and it produces stale reads, double fetches and race conditions that
only show up on a slow connection — which is exactly the connection your users are on while a
15-second LLM call runs.

`useEffect` is for synchronizing with something **outside** React: a subscription, a timer, an
imperative editor instance, a `beforeunload` handler. Not fetching. Not deriving. Not "when this prop
changes, update that state".

## Structure

```
web/src/
  api/            one typed function per endpoint. NOTHING else calls fetch.
  features/<slice>/
    components/   presentational + container components for this slice
    hooks/        useTailoringRun.ts, useExportJob.ts …
    types.ts
  components/     genuinely shared, presentational, no fetching
  hooks/          cross-feature only
```

The rule that keeps this honest: **a component never calls `fetch`, and never imports `axios`.** It
calls a hook, which calls the client, which is the only thing that knows about HTTP.

## Custom hooks

A hook exists to encapsulate **behaviour with a lifecycle** — a query plus its invalidation, a polling
loop, an editor instance — not to hide three lines. Name it for what it gives you (`useTailoringRun`),
type its return explicitly, and keep JSX out of it.

```ts
export function useTailoringRun(runId: string) {
  return useQuery({
    queryKey: ['tailoringRun', runId],
    queryFn: () => api.getTailoringRun(runId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 1000 : false),
  });
}
```

That `refetchInterval` returning `false` when the run finishes is the whole polling pattern. No
`useEffect`, no `setInterval`, no cleanup you can forget.

## The three states, always — and here the loading state is the feature

Every screen that touches the network renders **loading**, **error** and **empty** deliberately. This
app has a 15-second happy path, so the loading state is not a spinner afterthought: it is the staged
progress the PRD asks for (*Extracting CV → Fetching Job → Tailoring with LLM*), driven by the run's
real status, not by a timer pretending.

Model the states as a **discriminated union**, not a `loading` boolean beside a `data` field — the
boolean lets you render "loading" and "ready" at the same time, and eventually you will.

The error state must distinguish **"this failed, try again"** from **"still working"**. A user who
cannot tell will refresh, and you pay for another LLM call.

## The editor: two tabs, one run

CV and cover letter are two TipTap instances over one `TailoringRun`. Rules:

- **Do not destroy and recreate an editor on tab switch.** Keep both mounted and toggle visibility, or
  hold the documents in state above them. Losing unsaved edits on a tab click is the bug users report
  as "it deleted my work".
- TipTap is an imperative instance living outside React — this *is* the legitimate `useEffect` case.
  Create it once, clean it up on unmount, and never re-create it because a prop changed.
- **LLM output is untrusted text** rendered into HTML. Sanitize it. If you write
  `dangerouslySetInnerHTML`, the sanitizer call is in the same expression or it does not ship.
- Autosave is debounced and its state is visible ("Saved" / "Saving…"). Silent autosave is
  indistinguishable from broken autosave.

## Downloads run on a worker

PDF and DOCX are rendered by Celery (ADR-0005), so a download is **not** a link to a file that may not
exist. Create the export job, poll its state with the pattern above, and only then offer the file —
with a visible failure path. A link that 404s because the worker is down is the worst version of this.

## TypeScript

`strict` is on and stays on. No `any`. No `!` to silence the compiler — if the type says it may be
undefined, handle it, because that branch is a real state your user will hit. Props typed explicitly.

## Never

- An access token in `localStorage` (ADR-0008). Memory + an HttpOnly refresh cookie.
- A business rule re-implemented in TypeScript. The API is the authority (Constitution §4.5); the
  frontend formats and pre-validates for UX only.
- A new state-management library without asking. The answer is almost always TanStack Query.

## Before you finish
`make web.check` — `tsc --noEmit`, ESLint, Vitest, and a production `vite build`. A build that fails
is a deploy that fails.
