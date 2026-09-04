# FOR boehpyk — The TailorCraft Story (So Far)

A plain-language companion to TailorCraft: what we're building, how the pieces fit, why we chose what
we chose, and the lessons hiding in those choices. Written to be read, not filed. It grows with the
project — right now it's the story of **day one: the harness**, because that's where the most
consequential decisions get made and the least code exists to distract from them.

---

## What TailorCraft actually is

Alex has just lost his job. He wants to apply to ten roles this week, and every single application
wants a slightly different CV and a cover letter that sounds like he read the posting. So he spends
thirty to forty-five minutes per application moving bullet points around, and by Thursday he's applying
to fewer jobs — not because there are fewer jobs, but because he's tired.

TailorCraft is a machine for deleting that thirty minutes. Drop in your CV. Paste the job posting (or
a link to it). Fifteen seconds later you have a tailored CV and a cover letter you can edit in the
browser and download as a PDF or a Word file. No account needed. If you want one, it remembers your
base CV so the next nine applications start from "paste the posting."

That's the product in one sentence: **one base CV in, one tailored application out, fast enough that a
person applies to ten jobs in an evening.**

There's a second product here, and the PRD is unusually honest about it: **this codebase is a school.**
FR-7 doesn't say "the code should be nice," it says the architecture must be good enough to *learn
Python and React from*. That's a requirement with teeth. It means a shortcut here doesn't just cost
maintenance later — it teaches the wrong lesson to the only person who will ever read it.

## The shape of the thing (architecture)

We're using a **hexagonal architecture** — Ports and Adapters. If that sounds fancy, here's the kitchen
analogy:

- The **domain** is the *recipe*. Pure ideas about the business, written so any cook in any kitchen
  could follow them. It knows nothing about your oven. In code: pure Python describing base CVs,
  postings, tailoring runs and their rules, with **zero** mention of FastAPI, SQLAlchemy, or even
  Pydantic.
- The **application** layer is the *cook*. It follows a recipe step by step ("tailor this CV to this
  posting"), and still doesn't care whether the oven is gas or electric.
- The **infrastructure** layer is the *actual kitchen*. The specific oven, the Postgres database, the
  Gemini API, the PDF renderer. Swappable appliances.

The trick that keeps this honest is a **port**: an interface the domain defines — "I need *something*
that can take a CV and a posting and give me back tailored documents," `LlmPort` — without knowing what
fulfills it. Today a Gemini adapter plugs in. Tomorrow, if Google triples the price or something better
appears, a different adapter plugs into the *same* port and the recipe doesn't change. Appliances
change; recipes don't.

For this product specifically, the port earns its keep on day one rather than in some hypothetical
future — and for a reason you might not expect. **It's what makes the failure paths testable.** The
real Gemini API is slow, non-deterministic, costs money per call, and refuses things occasionally. You
cannot write a test that says "when the model rate-limits us, the user sees a retry prompt" against the
real API — you'd have to *get yourself rate-limited on purpose*. With the port, you write a
five-line fake that raises `LlmRateLimited` and you're done. The abstraction people usually justify with
"what if we switch vendors" pays for itself immediately with "what if it breaks."

## Three decisions worth remembering (because you almost went the other way)

This is the part to reread in six months, because it's where the *thinking* lives.

### 1. Pydantic is banned from the domain, and that felt wrong for about ten minutes

Pydantic is *great*. It validates, it's fast, it can be frozen, and every FastAPI tutorial on earth
puts it everywhere. The obvious move is to make your domain objects Pydantic models and get validation
for free.

We didn't. Domain value objects are frozen `@dataclass`es that validate in `__post_init__`.

Why? Because the moment `BaseCv` is a `BaseModel`, your business rules live in the same class as your
JSON field aliases, your serialization config, and a validation error type designed for HTTP responses.
The domain starts making decisions about wire formats. And a Pydantic major-version bump — v1 to v2 was
effectively a rewrite — becomes a change to your *business model*.

The cost is real: you write the mapping from Pydantic request schema → domain object by hand. That's
friction in week one. It's also the exact reason the design survives to month six. **The layer where
meaning lives should not also be the layer where the wire format lives.**

### 2. SQLAlchemy is used in a way almost nobody blogs about

Every SQLAlchemy tutorial shows you *declarative* mapping: `class BaseCv(Base)`, with `mapped_column()`
on each field. It's clean, it's popular, and it's an ORM import sitting inside your domain class. The
design would be over before it started.

So we use **imperative mapping** instead — a mode SQLAlchemy has supported forever and mentions almost
in passing. You define a plain Python class in `domain/`. Separately, in `infrastructure/`, you define a
`Table` and call `registry.map_imperatively(BaseCv, base_cv_table)`. SQLAlchemy wires them together at
startup. The domain class never learns it's persisted.

This is the single most likely place for the architecture to quietly collapse, precisely *because* the
wrong way is the well-known way. Which is why it isn't left to memory — see below.

### 3. Willpower is not a boundary; tools are

The layer rules are enforced by **import-linter**, which fails the build if `domain/` imports anything
third-party. Good engineers don't trust themselves to remember rules at 1 a.m.; they make the rules
mechanical.

But we went one step further than the previous project did, and the reason is instructive. Muzbar's
tooling doc listed a Claude Code hook — a guard that would block a bad import *at write time* rather
than at commit time — as a Phase 0 item. Four features later, it still wasn't built. Nothing terrible
happened; the commit-time gate caught things. But "we'll wire the fast guard later" is how you end up
running on the slow guard forever.

So here, `.claude/hooks/domain-purity-guard.sh` was written on day one and it works: try to write
`from pydantic import BaseModel` into a domain file and the edit is **refused**, with a message telling
you where that import actually belongs. Ten minutes of shell scripting, and a whole category of mistake
stops being possible.

## What we inherited from muzbar — and what we deliberately changed

This harness is a port of the SDLC from your muzbar.com project: the Constitution, the ADR discipline,
the `/plan → /implement → /verify` loop, the small roster of sharp agents, the read-only reviewer, the
tracked git hooks. That machinery is good and it transferred almost unchanged.

But four things were changed *because muzbar wrote down what went wrong*, and this is the real payoff
of keeping a file like this one:

**1. The Claude Code hooks are wired now, not listed.** See above.

**2. Sentry is in Phase 0, not deferred.** Muzbar deferred error tracking as "cheap insurance, later,"
then spent an entire session debugging a crash-looping background worker while `make check` and the
health endpoint both reported green. TailorCraft's *first* slice already has that shape of hazard — an
LLM call that can fail slowly, a background render whose only failure symptom is a download that never
appears. Deferring it here would be repeating a mistake we have the notes on.

**3. `/health/ready` probes Celery, not just the datastores.** This is muzbar's lesson stated as a
design rule: **a stopped worker looks exactly like a healthy system** if you only check Postgres and
Redis. The API answers. The database answers. And every export queues silently forever. Probe the thing
that can be down.

**4. The deploy verifies the running image of *every* container.** Muzbar's deploy updated the web
container but not the background worker, which therefore ran a stale image **for four releases**. The
only symptom was behaviour that didn't match the source — the worst kind of bug, because you debug the
code in front of you and the code in front of you is fine. Our deploy pulls `api`, `worker` and `beat`,
and then *asserts* each one is running the SHA that was just released, failing loudly if not.

There's a meta-lesson in item 4 that's worth more than the fix. Muzbar's notes record that this bug was
first deferred with sound-sounding reasoning ("nothing in the next phase adds a second daemon"), and
then pulled forward the same day when someone noticed the trigger was simply *wrong* — the danger wasn't
a second daemon, it was any feature touching code the worker runs, which was the very next feature.
**When you defer a known bug, check what the next slice actually does, not what your stated trigger
condition says.** A trigger condition is a guess you made before doing the work that meets it.

## The stack, and why each piece is there

**Backend: Python 3.13 + FastAPI.** Async, because the core operation is one long wait on somebody
else's server. FastAPI's dependency injection is a natural composition root for ports-and-adapters — a
route declares the port it needs, and the wiring hands it an adapter.

There's a trap in that word "async," and it's the most likely performance bug in this codebase: PDF
rendering, PDF parsing and DOCX generation are all **synchronous and CPU-bound**. Call one inside an
async route and you block the event loop for *every* user, not just the one downloading. It works
perfectly with one user and falls apart with five, and it presents as "the app feels slow" rather than
as an error. That's why the reviewer treats it as a critical finding rather than a style note, and why
heavy rendering lives in a Celery worker.

**Celery + Redis** for that worker, plus a scheduler for the 24-hour guest data purge. `BackgroundTasks`
would have been simpler and would have lost the work whenever a container restarted — fine for logging,
wrong for a file someone is waiting on.

**PostgreSQL 16.** Boring on purpose.

**React 19 + TypeScript + Vite + Tailwind + TanStack Query + TipTap.** TanStack Query deserves a
sentence: the most common React mistake in an app like this is caching server data by hand in
`useState` and syncing it with `useEffect`. It produces stale reads and race conditions that only show
up on a slow connection — which is exactly the connection your user is on during a fifteen-second LLM
call. Having the right tool from day one means never learning the wrong habit.

**One VDS, Docker Compose, behind the Traefik you already run.** No Kubernetes, no managed anything, no
second console. The `FileStorePort` is the seam that makes S3 a later adapter rather than a migration,
if it ever needs to be.

## The thing this product has that muzbar didn't: a privacy problem

A CV is *dense* personal data. Name, address, phone number, every employer for a decade — often more
PII per kilobyte than anything else a person will ever upload. And it's handed over by someone who is
unemployed and in a hurry, which is not a person you get to be careless with.

So a few rules are absolute, from the first line of code:

- **Never log CV text, prompt bodies, or model completions.** Log ids, sizes, durations, token counts.
  A debug log is the easiest way to leak a stranger's home address into a file nobody thinks of as a
  database.
- **Never put a CV body in a domain event.** An event payload reaches every listener and every log line
  at once.
- **Guest data dies in 24 hours**, enforced by a scheduled job, not by hope — and the job gets a
  dry-run mode, a `--limit` for a cautious first bite, and a log line on *every* run including the ones
  that delete nothing. Because a job whose only failure symptom is silence needs a signal that a
  do-nothing job can't fake. "No output" must never be ambiguous between "healthy and idle" and "not
  running."
- **A guest session is not a weak login.** Having a session id is not authority over an object that
  merely references it. Check the link, in the use case, every time. The day those two ideas get
  conflated is the day a guest reads someone else's CV.

And the honest one: **the LLM provider sees the CV.** That's unavoidable in this product. So it gets
*said* — plainly, in the UI, at upload time — rather than buried in a privacy policy. Stating it also
turns out to be the natural moment to offer the account: *we don't keep your CV; register if you want
us to.*

## The thing that's hardest to test, and what we do about it

Most of this product's value passes through a dependency that gives a different answer every time. That
breaks the normal testing reflex, so we split the problem in two and are honest about the split:

- **Plumbing is tested.** Does a rate-limited response become a retry prompt? Does unparseable output
  get recorded as a failure instead of saved as content? Does a timeout leave the run in a state the
  user can retry? All of that is deterministic, and all of it is tested against fakes. CI has no Gemini
  key at all — deliberately, so a suite can never silently start spending money the moment someone adds
  one.
- **Quality is *evaluated*, not tested.** Whether the tailored CV is any good — whether it uses the
  posting's real requirements, keeps the candidate's real history, and produces a cover letter a human
  would actually send — is judgement. It gets a committed corpus of real postings, a `make eval` you
  run by hand when the prompt changes, and your own eyes.

The discipline is refusing to blur those two. A green test suite tells you the pipes work. Dressing
judgement up as a passing assertion is how you ship a confident, useless feature.

## Where we are right now

The harness is done, and so is the skeleton. `api/` is a real FastAPI application with the three
hexagonal packages, `uv`-managed dependencies and a health endpoint; `web/` is a real React app that
renders that health endpoint's report. Every gate has been *run*, not just written: Ruff, mypy
`--strict`, import-linter, 27 passing Python tests, TypeScript, ESLint, Prettier, 4 passing Vitest
tests, a production build, and a production API image that boots as a non-root user.

**And there is still no product code.** No aggregate, no use case, no migration. `domain/` contains
a `Clock` protocol, a `DomainEvent` base and an error class — about eighty lines, all of which will
outlive every feature built on top of them.

## Day two: four bugs, and the one thing they have in common

Everything above went from "written" to "verified", and that transition found four real bugs. All
four are worth keeping, because each is a small lesson about where confidence comes from.

**1. The virtualenv was in the wrong place.** The Dockerfile put it at uv's default, `/app/.venv`.
The dev override bind-mounts `./api` over `/app` — so the container's carefully built environment
would have been *shadowed* by whatever the host happened to have at `api/.venv`, which points at a
host interpreter path that does not exist inside the container. The symptom would have been a
missing-interpreter error that reads like a corrupt image. The fix is one environment variable
(`UV_PROJECT_ENVIRONMENT=/opt/venv`), and the general lesson is: **a bind mount does not merge with
the image, it hides it.** Anything the image builds must live somewhere the mount cannot reach.

**2. The test suite passed one test at a time and failed as a suite.** The engine fixture is
session-scoped; pytest-asyncio's default is a fresh event loop *per test*. An asyncpg connection is
bound to the loop it was born on, so tearing down a session-scoped engine on test 27's loop produced
`RuntimeError: got Future attached to a different loop`. Every test passed in isolation. This is
the most dangerous shape a bug can have — **it disappears exactly when you try to reproduce it in
the smallest case** — and no amount of reading the code would have found it. Running it did, in
about four seconds.

**3. A test asserted the wrong thing, confidently.** `test_domain_module_does_not_import_other_layers`
searched the file's raw text for the string `tailorcraft.infrastructure`. It went red — not on an
import, but on a *docstring in `clock.py` that mentions where the adapter lives*. A test that
punishes you for documenting something is worse than no test: it would have taught the lesson
backwards. The fix was to parse the imports instead of grepping the text.

There is a rule in this repo's CLAUDE.md that says a test must encode what the code *should* do,
never what it was observed doing. This was the neighbouring failure: a test encoding what the author
*assumed* the code does. Same cure — go and check.

**4. Ruff wanted to break dependency injection.** The `TC` rules move imports "only used in
annotations" into an `if TYPE_CHECKING:` block, which is excellent advice in most Python and
actively wrong here: FastAPI calls `get_type_hints()` at runtime to decide what to inject, and
Pydantic builds validators from annotations. Following the linter would have produced a `NameError`
at startup. **A lint rule is a heuristic with an author who did not know about your framework** —
when it and the runtime disagree, the runtime is not the one that is wrong.

The common thread: **every one of these four was invisible to reading and obvious to running.**
Three of them would have been discovered by a future me at a much worse moment — the venv one on
first `make up.dev`, the loop one on the first CI run with more than one test, the DI one on the
first deploy. The cheapest possible time to meet a bug is the minute after you write it.

## A note on what got left out, on purpose

Phase 0's roadmap entry mentioned a fake `LlmPort` for tests. It is not there, and that is a
decision rather than an omission: `LlmPort` is *domain* code in the `tailoring` context, and it does
not exist yet. Building a fake for a port with no definition would be guessing at a contract — the
exact speculative work the Constitution's non-goals list exists to prevent. It lands with slice 1.3,
as a task on that slice's list.

Two other things were deliberately declared but not implemented: `make purge.dry` and `make eval`
resolve to real CLI subcommands that print "this arrives with slice 1.6 / 1.3" and exit non-zero.
That is better than a `ModuleNotFoundError`, which reads like a broken install rather than an
honest "not yet".

The temptation in a scaffolding phase is to build the shape of everything and fill it in later. The
trouble is that a shape built before the thing it holds is a guess wearing the costume of a
decision. Empty is more honest than approximately right.

## What's next

Slice 1.1, `intake-base-cv-upload`: a user drops in a PDF, and text comes out. It is the first real
domain code in the project — a `BaseCv` aggregate, a `FileRef`, an `ExtractedText` value object, a
`CvTextExtractorPort` — and the first time the architecture has to carry actual weight rather than
describe itself.

The specs die when the features ship. This file doesn't.
