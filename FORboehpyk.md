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

## Day three: teaching the process to distrust itself

The SDLC already had the expensive half of TDD and nobody had noticed. Every feature spec enumerates
measurable acceptance criteria and a failure-contract table before a line of code is written, and a
human signs it off. That *is* "think about the behaviour first" — the part that actually makes TDD
work. What was missing was smaller and meaner: proof that a test can fail.

The gap is easiest to see in the rule that was already written down: *a test encodes what the code
should do — never what it was observed doing.* Good rule. Completely unenforced. It sat in the `qa`
agent's instructions as an appeal to virtue, and the `qa` agent was called **last**, with the fresh
implementation sitting right there in its context window. Asking anything — human or model — to write
an independent test about 200 lines it just read is asking it to un-know something. A human drifts
into it slowly and feels vaguely guilty. A model does it immediately, thoroughly, and reports green
with total confidence. The test then agrees with the code forever, because it was *derived* from the
code, and a test that cannot disagree with the implementation is decoration.

Red-first fixes that structurally rather than morally. You cannot record behaviour that does not
exist yet.

**But the naive version of this is theatre, and it is worth understanding why.** "Write the test
first, watch it fail" sounds complete until you watch what it actually fails *with*:

```
ImportError: cannot import name 'BaseCv' from 'tailorcraft.domain.intake'
```

That is red. It is also worthless. It proves one thing — a file does not exist — and says nothing
about whether the assertion you wrote can tell correct behaviour from incorrect. You could assert
`2 + 2 == 5` and get the same beautiful red. Ship that cycle a hundred times and you have a hundred
tests that were each "proven" by a missing import.

So the cycle here has three steps, not two. The implementer writes a **skeleton** first — real names,
real signatures, real types, `NotImplementedError` in every body. Now `qa` writes the test and it
fails like this instead:

```
Failed: DID NOT RAISE <class 'tailorcraft.domain.intake.errors.EmptyCvText'>
```

*That* is a red worth having: the assertion ran, discriminated, and came back negative. And because
it's worth having, it gets recorded — pasted verbatim into the RED commit body. An unrecorded red
did not happen.

The second thing worth stealing from this: **we deliberately did not apply it everywhere.** The
tempting move was blanket TDD, all layers, no exceptions — it sounds more rigorous and it reads
better in a document. It would have been worse. Writing tests against a SQLAlchemy imperative mapping
or an Alembic migration *before* you have discovered how those libraries actually behave means
writing the test wrong and rewriting it once reality arrives. That is not discipline, it's churn, and
it's especially wasteful in a codebase where learning the library is a stated goal. TDD quietly
assumes you already know the shape of the thing you're driving toward. In `domain/` you do — you
designed it. In `infrastructure/` you often don't yet.

So the line falls on **who owns the contract**. Domain rules, use cases, failure contracts, HTTP
status codes, "the user can tell 'still working' from 'this failed'" — those come from the spec, and
you can write the test first because you already decided the answer. Mappings, migrations, adapters,
DI wiring, markup — those come from the framework, and you find out what they look like by building
them. Red-first for the first list. Test-after, unapologetically, for the second.

Three smaller things fell out of the change, each a small lesson of its own:

**An escape hatch that skips tests will be used to skip tests.** A red commit can't pass `make check`
— its new test is *supposed* to fail — so it needed a way through the pre-commit hook. The lazy
answer is `git commit --no-verify`, and it's wrong twice over: it also drops the secret guard, the
PII guard and the published-port guard, none of which have anything to do with the test being red.
The answer instead was a narrower door: `make check.static` runs every gate except pytest and vitest,
and `TDD_RED=1` only opens it **when a test file is actually staged**. Design the hatch so it can only
be used for the thing it was built for, because otherwise, in six months, tired, you will use it for
everything.

**Enforcement went into the git history, not the diff.** The way this practice really dies isn't
someone skipping the red — it's the implementer, stuck on GREEN, quietly editing the test until it
passes. The end state looks perfect: a test, an implementation, all green. The diff at review time
shows nothing wrong. Only the history shows it — a RED commit, then a GREEN commit that touched the
test file. So the reviewer now runs `git log -p --follow` on test files and treats that signature as
CRITICAL. Some invariants are only visible in the sequence of states, never in the final one.

**And we wrote down what it does *not* cover.** The highest-risk items in this project are the
infrastructure footguns — the Traefik network pin, the worker running a stale image for four
releases, the migration that isn't backward-compatible. Not one of them has a failing test that turns
green. Adding a satisfying ritual is exactly when you're most likely to feel covered in areas the
ritual never touched, so the tier table says so out loud. A green suite is evidence about the things
it tests. It is silent about everything else, and silence is easy to mistake for approval.

## Day four: the first real feature, and five bugs that only running could find

Slice 1.1 shipped. You can open the page, drop a PDF on it, and watch it come back as
"999 characters extracted, stored until 9 Sept 21:45". Thirty-seven commits, 220 backend tests and
32 frontend ones, and the architecture finally carrying weight instead of describing itself.

The interesting part isn't that it worked. It's the five things that were wrong while everything
looked fine.

### 1. The test suite was migrating the wrong database

The rule "never the dev DB" is in CLAUDE.md in bold. The suite obeyed it in spirit and violated it in
fact: `conftest.py` politely set `sqlalchemy.url` to `tailorcraft_test` before calling
`command.upgrade`, and `alembic/env.py` politely overwrote it from the `lru_cache`d settings, which
had been populated with the dev URL long before the test fixture existed. Two pieces of code being
careful in opposite directions.

Nobody noticed for a whole phase, because **the bug had nothing to migrate.** Phase 0 had zero
migrations, so the wrong-database write was a no-op against an empty list. It became real and visible
in the same hour the first migration was written. The proof was embarrassingly simple once suspected:
run the suite, then look in both databases. Dev had the tables. `tailorcraft_test` had none.

The lesson isn't "check your Alembic config". It's that **a safety rule with no observable
consequence is not yet a safety rule** — it's a belief. This one became testable only when it
acquired something to break.

### 2. The streaming upload cap did not stream

`_read_capped` read the upload in 64 KiB chunks and aborted the moment the running total crossed
10 MB. Its docstring said so. The acceptance criterion said so — "aborted **while streaming**, not
buffered and then measured". A reviewer flagged it anyway, and the measurement settled it:

```
curl -F "file=@11mb.pdf" localhost:8000/api/base-cvs
  -> 413, bytes_uploaded=11000202
```

The whole 11 MB went up the wire, and *then* got refused. The cause is one of those framework facts
that is obvious in hindsight and invisible in the code: declaring `file: Annotated[UploadFile, File(...)]`
makes FastAPI call `await request.form()` **during dependency resolution**, before your function
body starts. Starlette's multipart parser drains the entire request into a temp file right there.
The careful chunked loop was reading bytes back out of a file that already held all of them.

The fix is a pure-ASGI middleware that checks `Content-Length` and answers 413 **without ever calling
`receive()`** — because not draining the receive channel is precisely what leaves a client that sent
`Expect: 100-continue` waiting for permission that never arrives. `bytes_uploaded` went 11000202 → 0.

Two things worth keeping. First: **a comment describing a guarantee is not a guarantee**, and this one
had been reviewed, tested and shipped while being false. The test that "covered" it asserted the 413
and honestly said in its own docstring that it could not observe the streaming half — which is how a
green suite and a false claim coexisted peacefully. Second, and more useful: through nginx the fix
changes nothing, because nginx answers `100-continue` itself before it has even opened the upstream
connection. So the honest description is three layers, not one, and the spec now says that instead of
the sentence it could not keep.

### 3. F-15 was unsatisfiable, and the spec had promised it anyway

The failure contract said: if the commit fails after the file is written, answer **503**. Reasonable.
Impossible. FastAPI runs the exit half of a `yield` dependency **after the response has been sent** —
documented behaviour since 0.106 — so `get_session`'s commit fires with the `201` already on the wire.
When it raises, Starlette sees `response_started` and the client keeps the 201 regardless. No
exception handler can fix that, because by then there is no longer a response to change.

The fix was to commit explicitly inside the handler's own error boundary, where a failure can still
become a status code. But the thing worth remembering is the *shape* of the discovery: a test was
written from the spec, it failed, and the failure was not in the code. **The spec was wrong, and the
test found it** — which is the entire argument for writing tests from acceptance criteria rather than
from implementations. A test derived from the code would have cheerfully asserted the 201.

### 4. The harness was lying twice

Both found by chasing that same F-15 test.

`conftest.py` overrode `get_session` — a *yield* dependency — with `lambda: session`. FastAPI calls
the override, sees something that isn't a generator, and uses the return value directly: the whole
`try/yield/except: rollback / else: commit` body never ran during any request in the suite. The
fixture's own docstring claimed "the code behaves exactly as it does in production". For the entire
HTTP path, it didn't. **Replacing a yield dependency with a plain callable silently deletes its
teardown**, and nothing fails — the suite just quietly stops testing something it says it tests.

And the API tests were writing real uploaded files into the shared dev volume: 706 of them, growing
on every `make test`. The database rolls back. The filesystem does not. It's the same lesson as the
Redis one already in CLAUDE.md, in the one volume the docs single out as load-bearing — and the proof
of the fix wasn't a passing test, it was counting files before and after: 706 → 706.

### 5. A date that rendered differently for every visitor

`formatStoredUntil` called `toLocaleString(undefined, {...})`. The spec's example said
`"8 Sep 10:00"`. On an `en-US` default it produced `"Sep 8, 10:00 AM"` — different word order,
different punctuation, different clock. `undefined` means "whatever this browser decides", so the
retention promise read differently depending on who was looking at it, and no test could pin it.

The `qa` agent found it, refused to write the test that recorded the wrong output, left the assertion
red and said so. That refusal is the whole system working: the rule "never write the test that
ratifies the accident" produced a red test and a decision instead of a quiet green.

### The one that got away, and what it cost

The frontend red-first cycle **degenerated**. The implementer was asked for a skeleton — real
signatures, `NotImplementedError` bodies — and delivered a complete, working component instead. So
when `qa` wrote the eight behavioural tests, all eight passed on arrival. No red. Nothing proven.

There is a strong temptation to call that a success (the tests exist! they're green!) and move on.
Instead: mutation testing. Delete the retention sentence — one test dies. Un-disable the upload
control — one test dies. Break pre-validation — two die. Collapse the "unreadable file" notice into
the "upload failed" notice — three die, including the AC-15 assertion that exists specifically to keep
them distinct.

That recovered most of the value and the commit says plainly that it is *not* the same thing: **a
mutation proves the test notices a change you thought of; a red proves the assertion discriminated
before the code existed.** One is a check on your imagination, the other is a check on the test.
Worth knowing which you have.

### The common thread, again

Day two's four bugs were all invisible to reading and obvious to running. Day four's five are worse:
**every one of them was invisible to reading, invisible to a green test suite, and obvious to
measuring.** The migration bug needed a look inside two databases. The upload cap needed
`%{size_upload}` from curl. The event-loop question needed a stopwatch on `/health/live` during four
concurrent uploads — which is how we learned that `asyncio.to_thread` buys liveness but not
throughput, because pypdf is pure Python and the GIL serialises it anyway (four concurrent
extractions came in *slower* than four serial ones).

The suite was green through all of it. It was green because it tested what it tested — which is all a
suite ever does, and is exactly why the tier table in the SDLC says out loud what it does not cover.

### Two places the spec contradicted itself

Both resolved on purpose, both written down, because the rule is "fix one of them and say which won".

**RTF.** F-4 said reject it; AC-3 defined text as anything decoding without a NUL — and RTF markup
decodes perfectly. F-4 won, via a `{\rtf` magic check, on the grounds that accepting it would hand
the model a CV made of `\rtf1\ansi\deff0` control words. An honest 415 naming the three formats
that work beats a successful upload that produces garbage.

**Blank TXT.** The technical plan mapped it to `EmptyExtraction` — which is not a `CvExtractionFailed`
subclass, so it would have escaped the use case's `except` and become exactly the 500-with-nothing-on-disk
that ADR-0004 exists to forbid. The plan lost to its own failure contract, which had already said
`too_short`.

## Day five: the review that found the bug the tests were built to miss

Slice 1.1 arrived at `/verify` looking finished. Every task ticked, 220 tests green, green twice in a
row, every one of the 24 failure-contract rows with a test pointing at it. The gates had nothing to
say. Then the reviewer found two CRITICALs in an hour, and neither of them was the kind of thing a
test suite finds — because both were bugs about the *shape* of the code, not its behaviour on any
input anyone had thought to try.

### The allow-list was a bet, and it was already losing

`CvTextExtractorPort` makes a promise in its docstring: raises a `CvExtractionFailed` subclass on
**every** failure. `UploadBaseCv` believes it — it catches that one type and nothing else. And the
adapter delivered on that promise by listing the exceptions it knew about: `PdfReadError`,
`BadZipFile`, `PackageNotFoundError`, `FileNotDecryptedError`.

Spot the assumption. That list is a claim to have enumerated every way two third-party parsers can
fail on a file a stranger chose. The reviewer tested the claim the only way it can be tested — it
corrupted 300 random copies of the sample CV and ran them all through. Twenty-one escaped:
`KeyError`, `AttributeError`, `ValueError`, `LimitReachedError`. Every one of those became a 500,
with the user's CV already written to disk and no database row pointing at it — the precise outcome
ADR-0004 exists to forbid, in the codebase that wrote ADR-0004.

Here is the part worth carrying to every other project you work on. The comment in the router already
said `EXTRACTOR_ERROR` was "the catch-all for a timeout **or an unexpected library failure**". The
enum member existed. The user-facing message existed. Everything about the design was right, and the
one line that would have made it true had never been written. **A comment describing behaviour no
code path produces is worse than no comment**, because it stops the next reader from checking.

The fix is four lines. The lesson is a habit: when you translate a third-party library's exceptions
into your own, the allow-list goes on top and a catch-all goes underneath. Not defensive
programming — the allow-list *is* the guess, and the floor is what makes the port's promise true by
construction instead of by optimism.

### The privacy bug hiding inside the availability bug

That would have been a MAJOR. What made it CRITICAL was where the escaping exception was going.

We were careful about Sentry. `send_default_pii=False`. `max_request_body_size="never"`. Both set in
Phase 0, both correct, and both entirely beside the point — because `sentry_sdk` also defaults
`include_local_variables=True`, and that is a *different* setting governed by nothing we had
configured. An exception escaping the extractor carries a traceback, a traceback carries frames, and
those frames held `data: bytes` (the whole CV), `raw_text: str` (the extracted text) and the original
filename. On any box with a DSN, one corrupt upload would have shipped a stranger's complete CV and
their name to a third-party service.

Two settings that *sound* like they cover PII, one that actually decides it. This is the anatomy of
most privacy failures: not an absent control, but a control whose name suggested a wider scope than
it had, next to a default nobody read. The next time you write `send_default_pii=False` and feel
covered, go and read what the library does with frame locals.

The fix was `raise CvExtractionFailed(...) from None`. And I checked the mechanism rather than
believing the explanation — `sentry_sdk.utils.walk_exception_chain` genuinely branches on
`__suppress_context__` and stops when `__cause__` is `None`, so the frame holding the CV is
unreachable from the report. Worth noting the reviewer's own footnote on it: with the catch-all in
place nothing escapes the use case anyway, so the `from None` protects a frame that can no longer be
reached. That is the right order to build defence in — the belt does not become pointless because
you also have braces.

### Four hundred milliseconds, and no error anywhere

The second CRITICAL: sniffing ran on the event loop.

Deciding whether an upload is a DOCX means opening it as a zip and reading its namelist, and reading
a zip's namelist means reading its entire central directory. So the cost of that "quick check" is
chosen by whoever uploaded the file. A crafted archive of 100,000 tiny entries — 8.6 MB, comfortably
under our 10 MB cap, not malicious in any way a scanner would notice — stalled the loop for **374
milliseconds**. Not that request. *Every* request, for every concurrent user, including the health
check.

What makes this the most dangerous class of bug in an async codebase is the failure mode: there
isn't one. Nothing errors. Nothing logs. The app is just slow, for everyone, occasionally, and you
will look for that in the database. We had already learned this lesson — extraction and every single
file-store syscall were correctly in `asyncio.to_thread`, and ADR-0009 even has a measurement proving
it. Sniffing sat ten lines earlier in the same function and nobody looked at it, because it "isn't
real work".

One `asyncio.to_thread` later: 374 ms → 50 ms. And the residual 50 ms is itself worth understanding —
that's GIL contention from pure-Python zip parsing, which is the same "threads buy you event-loop
liveness, not throughput" effect ADR-0009 already recorded. Knowing which number is a fix and which
is a known ceiling is the difference between engineering and cargo cult.

I had also argued to myself that the rate limiter made this safe. It doesn't, and the distinction is
worth keeping: **a rate limit bounds how often the loop is stalled, never whether it is stalled.**
Thirty 400 ms stalls per IP per hour are still thirty stalls.

### The agent that found the second instance

Small thing, big signal. I handed the fix to `api-dev` with the extraction bug described. It fixed
that — and then went and found the *same class of bug* in `_is_docx`, which caught only `BadZipFile`
while its own docstring promised "this function never raises". A malformed zip central directory
raises `struct.error`, `NotImplementedError('zip file version 17.2')`, `EOFError` and others
depending on which field the corruption lands in. Each one was a 500 for a file whose only crime was
not being a DOCX — which is a 415.

Being handed one instance of a bug and returning with the class is the behaviour you want from a
colleague, and it is worth naming when you see it.

### The test that could not fail

The most instructive finding of the day was the smallest, and it is about testing rather than code.

AC-12 says nothing in this slice logs CV text. There was a test. It uploaded a clean PDF, grepped the
logs for the fixture's name and email, and passed.

It could not have done anything else. `pypdf` logs through the standard library from inside the
extraction thread, and it only does so on files that are *damaged but still parseable* — and at least
one of those call sites formats `repr()` of a raw line lifted straight out of the document. The test
uploaded a clean file, so it never went near the only code path capable of producing the leak it
claimed to guard. It was green for the same reason a smoke detector in a sealed box is quiet.

The leak is real, and we captured it:
`PdfReadError("Invalid Elementary Object starting with b'\x8a' @223: ...(Alex Rivera) Tj E'")` —
a candidate's name, mid-log-line, from a library we do not control, straight into stdout.

Then it got better. Writing the honest version of that test, `qa` discovered that
`alembic/env.py`'s generated `fileConfig(...)` boilerplate sets `.disabled = True` on **24**
pre-existing loggers — `pypdf`, `docx`, `celery`, `redis`, `sqlalchemy`, `sentry_sdk`, `httpx` —
none of which `alembic.ini` mentions. The migration fixture is session-scoped, so one migration run
silenced those loggers for the entire suite. Which means: *any* test asserting "X never appears in
the logs" could pass because nothing was logging at all, and any future test asserting something
**is** logged would fail for a reason nobody would find quickly.

Two independent silencing mechanisms, one deliberate and one accidental, and the accidental one was
strong enough to make the deliberate one untestable. `qa` caught it, undid both explicitly in the
test, and — this is the part I want to remember — **refused to ship the green version**, saying so in
the report rather than presenting five passing tests. It also volunteered that the extended tests on
the committed corrupt fixtures stayed green even with the guard removed, so the real discrimination
rested on one crafted seed. Nobody would have caught that from the outside.

Fixed at the source: `disable_existing_loggers=False`, with a comment explaining why a line of
generated boilerplate was load-bearing. A trap that every future author has to *remember* is a trap
with a longer fuse.

### The red that wasn't

One process failure, self-reported, and it belongs here precisely because nothing went wrong
visibly.

The frontend behavioural tests (T34) are a red-first tier. They never went red. The "skeleton" from
T33 arrived as a working implementation with the plan's exact sentences already written into it, so
all eight tests passed the moment they were written. A test that has never been observed failing has
not been shown to discriminate — and worse, when the implementation already exists, the
implementation rather than the acceptance criterion quietly becomes the thing the assertion is
copied from.

The commit says all of this in its own message instead of claiming a red it did not get, and
substitutes four documented mutations — delete the retention sentence, un-disable the button, break
each error branch — each confirmed to kill the right tests. That is real evidence, and it is weaker
in one specific way the commit names better than I would have: *a mutation proves the test notices a
change you thought of; a red proves the assertion discriminated before the code existed.*

The correction for 1.2 is one sentence: **a skeleton for a red-first React tier renders the structure
with placeholder copy, never the finished sentences the test is about to assert.**

### The common thread, a third time

Day two's bugs were about state you forgot existed. Day four's were about the harness checking
something other than what it claimed. Day five's are about **promises**.

Every finding was a place where something asserted a guarantee it did not have: a port's docstring
promising to translate every failure, backed by a list of four; a function promising "never raises",
backed by one exception type; a Sentry setting whose name implied it covered PII; a comment
describing a catch-all that didn't exist; a test whose name promised it guarded a privacy rule it
could not reach; a red-first cycle that produced no red. In each case the code and the claim about
the code had drifted apart, and only the claim was visible to a reader.

Which is the argument for the whole apparatus — the reviewer, the mutations, the sweeps. Tests tell
you the code does what it does. Almost nothing except a second pair of eyes tells you the code does
what it *says*.

One habit generalises out of all six: when you write a promise down — in a docstring, a comment, a
test name — ask what would have to be true for it to be false, and then go and check that. That is
the entire method, and it found two CRITICALs in a slice that passed every gate we had.

## What's next

Slice 1.2, the job posting: a URL goes in, a description comes out, and with it the SSRF guard —
scheme allow-list, no private ranges, re-check on redirect. It is the first time this application
makes an outbound request on a stranger's instruction, which is a different and more interesting kind
of dangerous than anything in 1.1.

The specs die when the features ship. This file doesn't.
