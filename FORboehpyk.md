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

## Day six: the outbound request, and four claims that measurement destroyed

Slice 1.2 shipped. You can paste a job description or hand the app a link, and when the link does not
work — which for about half the job boards people actually use, it will not — you get a button that
switches you to pasting with the URL still on screen, rather than a sentence suggesting you do that
yourself.

Twenty-nine commits. But the number worth remembering from this slice is **four**: the number of
things I wrote down as fact, in a docstring or a comment, that turned out to be false when I went and
checked. Not typos. Confident, plausible, load-bearing claims — the kind a reviewer nods at.

### The one that mattered: what an IPv4-mapped address actually does

Every SSRF guide tells you to unwrap `::ffff:127.0.0.1` before judging it, and the reason given is
always the same: as an IPv6 address it is not `::1`, so `is_loopback` is False and it sails through.
I wrote that in the docstring. It reads well. It is wrong:

```
IPv6Address("::ffff:127.0.0.1").is_loopback         -> True
IPv6Address("::ffff:10.0.0.1").is_private           -> True
IPv6Address("::ffff:169.254.169.254").is_link_local -> True
```

CPython already resolves mapped addresses for every property the policy uses. So the received wisdom
is not just imprecise, it is backwards — and I had copied it into a security control's documentation.

Then the sharper question: **is the unwrap doing anything at all?** I deleted the line and ran the
suite. All thirty-one tests passed. The line was decoration as far as the tests were concerned, and
the test named `blocks_ipv4_mapped_loopback` passed on CPython's behaviour rather than on ours — a
test whose docstring claimed to guard a regression it structurally could not catch.

It turns out the unwrap *is* load-bearing, for exactly one address, for a reason nobody would guess.
`100.64.0.0/10` — carrier-grade NAT — is the one range `ipaddress` has no property for, so the policy
checks it by hand against an `IPv4Network`, gated by `isinstance(address, IPv4Address)`. Without the
unwrap, `::ffff:100.64.0.1` arrives as an `IPv6Address`, skips that gate, matches nothing, and is
allowed. One address in the whole space. The tests now cover it, and I proved they discriminate by
deleting the line again and watching exactly those two fail.

**The lesson is not "verify your security code".** It is narrower and more useful: *a line that every
tutorial tells you to write is the line least likely to be tested*, because everyone including you
already believes it works.

### The bug the tests could not have found

The fetcher passed everything. Then I pointed it at a stub server that answers
`302 Location: http://169.254.169.254/latest/meta-data/` — the cloud metadata endpoint, the thing
SSRF exists to reach.

It came back `fetcher_error`, after a three-second connect timeout.

Read that again, because the distinction is the entire slice. The guard had **not refused it.** The
socket had simply failed to open, because there is no metadata service on a laptop. On a real cloud
box that socket connects, and the response is credentials.

The cause was in the test seam itself: `allow_private=True` short-circuited to allow *everything*, so
the permissive policy the adapter's own tests use could reach the metadata endpoint — and AC-7, the
criterion that says a redirect to a blocked range must be refused at the hop, was untestable by
construction.

My first fix was also wrong, and wrong in a way I would not have predicted: narrowing the permissive
branch to `is_loopback or is_private` still let it through, because **`IPv4Address.is_private`
includes link-local.** `169.254.169.254` is "private" as far as the standard library is concerned.

So the permissive policy now widens four *named* ranges — loopback and the three RFC 1918 blocks —
and nothing else. Which buys a property worth stating plainly: **no policy this codebase can
construct, strict or permissive, in `src/` or in a test, will connect to a link-local, multicast,
reserved or CGNAT address.** The address SSRF exists to reach is out of reach in every configuration,
including the convenient ones.

### SQLAlchemy quietly removed an invariant

`JobPosting` has two named constructors and no `__init__`, and the absence *is* the mechanism: call
`JobPosting(...)` directly and you fall through to `object.__init__`, which rejects keyword arguments.
That is what makes invariant J-2 — `source == FETCHED` iff `source_url is not None` — unbreakable
rather than merely checked. `BaseCv` and `GuestSession` say the same thing.

It stopped being true the hour the imperative mapping was registered. `registry.map_imperatively`
installs a default constructor on a mapped class *that does not define one*, and that constructor
accepts the mapped attribute names. So this became legal:

```python
JobPosting(_source=PASTED, _source_url=SourceUrl("https://x.com/j"))
```

A third way to build one, setting the two fields independently — precisely the state the design makes
unrepresentable everywhere else.

What makes this a good story rather than an embarrassing one is *how* it was caught. `qa` had written
the guard test three commits earlier, and its docstring read: "Someone adding an `__init__` later —
for a test fixture, **for SQLAlchemy**, for convenience — would dismantle that quietly, and this
assertion is the thing that notices." It named the culprit before the culprit arrived.

Two fixes failed before the right one. A *raising* `__init__` breaks the named constructors, because
a mapped class must be built through `cls()` — SQLAlchemy's instrumentation wrapper is what attaches
`_sa_instance_state`, and `cls.__new__(cls)` dies with `'NoneType' object has no attribute 'set'` on
the first assignment. A private sentinel parameter works and is persistence leaking into the domain.
The answer was an `__init__` that takes nothing and does nothing: the mapper leaves a user-defined
constructor alone, so every argument is a `TypeError` again.

`BaseCv` and `GuestSession` had carried the same hole since 1.1, with comments asserting a guarantee
they had stopped providing. Three hundred and forty-four tests passed before and after the fix —
which is the finding, not a footnote. **Nothing was using the hole. It was simply a promise three
docstrings made and none of them kept.**

### A spec ambiguity, caught by writing the test first

`JobPostingText` caps at 30,000 characters. The feature spec said "characters after normalization";
the technical plan, the error class and my own skeleton docstring all said "**non-whitespace**
characters after normalization". Different numbers for any real posting — six thousand words of
`aaaaa` is 30,000 non-whitespace and 35,999 long.

`qa` found it while writing the boundary test, and — this is the part that matters — **stopped**
rather than picking one and moving on. A test that ratifies whichever reading the implementation
happens to use has no source of truth independent of the code.

The tie-breaker came from a place neither document mentioned: the UI shows a live counter reading
`3,184 / 30,000`, fed by `character_count`. Measure the ceiling in the other unit and we accept text
the counter then renders as **"35,999 / 30,000"** — a limit visibly exceeded by input we just called
fine.

So the two bounds now measure different things, on purpose: the floor counts non-whitespace, because
whitespace is not content and three hundred blank lines must not sneak past a minimum meant to
guarantee some; the ceiling counts what the counter counts. Three sources said one thing, one said
the other, and the one won — so the reasoning went into all four places rather than just the number.

### What the four claims have in common

The `ipv4_mapped` justification. The "injectable resolver" I documented and never built. `is_private`
not covering what its name suggests. A comment from the first commit saying normalization "removes"
whitespace when it *collapses* it — which two commits later made a shared predicate reject
"Senior Python Engineer" for containing a space.

None was a mistake in the code. All four were mistakes in the *description* of the code, and every
one of them survived a review that only read the code. Day five's chapter ended on the observation
that almost nothing except a second pair of eyes tells you the code does what it says. Day six
narrows it: **the second pair of eyes does not have to be a person.** Three of these four fell to a
single move — take the sentence, work out what would be observably true if it were false, and run
that.

The fourth fell to a stub server, which is the same move with a socket.

### The other thing worth keeping: a fixture trap

Seeding rows with `session.flush()` and then making two requests in one test loses the rows. The
first request's exception — including a perfectly legitimate 404 — triggers the test session
override's rollback, which discards flushed-but-uncommitted fixtures before the second request runs.
Use `commit()` in fixtures; under `join_transaction_mode="create_savepoint"` that only releases a
savepoint, so the outer rollback still isolates the test.

It cost twenty minutes and it will cost twenty more in some future slice, which is why it is written
down here rather than in a comment in one test file.

## Day seven: the first slice that spends money (the backend half)

Every failure before this slice cost CPU and disk. Slice 1.3 changes that. A CV and a job posting
go in, a tailored CV and a cover letter come out, and each request can now **cost real money**
and take twelve seconds. Think of a restaurant that starts charging per order: from then on, an
order lost in the kitchen is no longer a shrug, it is a refund.

This chapter covers the backend: the domain, the use cases, persistence, the Gemini adapter, the
queue and the HTTP contract. They are all committed and green, with 707 backend tests. The React
surface, the first real evaluation against the API and the final review are still ahead.

### The question that settled four questions

The biggest decision was whether tailoring runs inside the HTTP request or on a queue the browser
polls. A synchronous call fits the 15-second budget and would have deleted eight tasks. It lost
on one scenario: **the user's connection drops at second eleven.** The model finishes, Google
bills us, and the response goes nowhere. Nothing is recorded, which is exactly the outcome ADR-0004
forbids. The result has to outlive the request that asked for it, so the work goes on a queue.
That added a second rule to ADR-0005's "queue it when it is expensive": **queue it when its result
must outlive the request.**

The same slice exposed an apparent contradiction. In slice 1.1 a failed text extraction became a
row with a failure status; in slice 1.2 a failed fetch left no row at all. Which one does
tailoring copy? Neither. One question decides it: **was anything spent, and is there an artifact
to own?** Before the job is queued, nothing has been spent, so a rejected request leaves no row.
After it is queued, someone is waiting and money is about to be spent, so **every** outcome is a
row, failures included. ADR-0014 records it, and the two earlier slices turn out to be two answers
to the same question.

### A constraint that compared a status to a conjunction

The spec defined a database check: `(status = 'succeeded') = (tailored_cv IS NOT NULL AND
cover_letter IS NOT NULL)`. It reads as "succeeded exactly when both documents exist", and it does
not mean that. A failed run holding exactly **one** document makes both sides false, and
`false = false` passes. Postgres accepted `UPDATE ... SET cover_letter = 'x'` on a failed run
without complaint.

Nobody found that by reading the SQL. The agent building the table tried the bad updates against a
real database, one by one. It also did the more important thing: **it did not quietly strengthen
the constraint.** It reported the gap and asked, because a table that disagrees with its own spec
is worse than either version. The constraint is now written once per document column, and the
feature spec, the technical plan and the task list were changed in the same commit, so the
migration and the tests could not drift from it.

The lesson generalises. **A guard whose only job is to stop hand-written SQL should be tested with
hand-written SQL.**

### The gate the spec asked for could not exist

AC-6 required forbidding imports of `google.genai` outside the adapter, via import-linter.
import-linter rejects that string before analysing any code: *"subpackages of external packages
are not valid."* The string that works is `google`. The agent proved it by planting forbidden
imports in both layers, using both spellings (`import google.genai` and
`from google import genai`), and watching the contracts fail before removing them.

A misconfigured forbidden-imports rule looks exactly like a working one in every passing build. **A
gate you have never seen fail is a smoke detector whose test button nobody pressed.**

### A worker running a virtualenv from two slices ago

`make deps` synced dependencies into the `api` container only. `worker` and `beat` were still
running a virtualenv from **slice 1.2**. Nobody noticed, because the only new library since then
was used by code that runs in the API. The Gemini call runs in the worker, where this would have
surfaced as a missing module in the one process nobody watches. It is the same failure as
muzbar's worker running a stale image for four releases, one level down.

### The worker had never configured its own logging

The logging setup and the Sentry setup were hooked into FastAPI's startup and nowhere else. The
worker, the process that holds a CV in memory for twelve seconds, **never ran either.** It had no
silencing of noisy vendor loggers, and the Gemini SDK is built on `httpx`, the exact logger slice
1.2 caught writing full URLs to the logs.

Sentry also had a gap. `send_default_pii=False` was set, and so was the request-body limit, but
`include_local_variables=False` was not. That third setting is the one that decides whether a
function's local variables, such as a whole CV, reach Sentry. CLAUDE.md's footgun list already
described this ("two settings that sound like they cover PII, one that decides it"), and the code
had the two that sound right. Both gaps are fixed.

### Three bugs the test suite could not see

These are worth studying, because in each case tests were written and would have stayed green.

**The exception that carried the whole document.** When the model returns invalid JSON,
`json.JSONDecodeError`'s message is harmless (`Expecting ',' delimiter`), but the exception object
keeps **the entire response** in its `.doc` attribute. Chain that into our own error and half a
tailored CV rides along to Sentry. The privacy tests only check the error's message and repr, so
they would pass either way. The fix is `raise ... from None`, and it came from reasoning about the
exception object, not from a failing test.

**The clock that would have zeroed the latency metric.** The worker reads the time when a run
starts and again when it succeeds. Reusing the first reading for the second would make every
production run report a duration of zero. Tests use a fixed clock, where both readings are
identical anyway, so no test can tell the difference. It was caught while the line was being
written.

**The test override that never reached the worker.** The spec says no test calls the real Gemini
API, and tests replace dependencies through FastAPI's `dependency_overrides`. No web route calls
the LLM, though: the Celery task does, and it builds its own objects in the worker's composition
root, which that override cannot reach. In CI, where no API key exists, this was safe by accident.
On a laptop with a real key in `.env`, the suite would have spent money while reporting green. The
tests now replace the LLM on the worker's path too, and **assert the fake was called** instead of
assuming it was.

### The comment that overstated its case

The startup guard that refuses to boot production without an API key raises a custom exception
instead of `ValueError`. The agent's justification was that a `ValueError` would print every secret
in the settings. Measured, pydantic does include the settings in the error, but truncates them, and
on the shape tested no secret got through.

The decision was still right, because "no secret got through" depended on how many settings exist
and in what order, and that changes whenever one is added. The comment was not right, though.
**An overstated justification gets tested by the next reader, disproven, and thrown out along with
the real reason.** It now says what was actually measured.

### Tests that pass for the wrong reason

Four of these, and the pattern matters more than any single one.

- **A cause that was never set.** A test checked that "not found" and "not yours" were
  distinguishable through the exception's `__cause__`. The positive check passes even if no cause
  is ever set, so it needed a negative twin. That twin only works if the real repository raises
  `from None`, which is now written into the repository's contract.
- **A privacy test that proved nothing had been captured.** Its only evidence of capture was "at
  least one log record exists", and any database log line satisfies that. It now requires both runs'
  ids and events to appear before checking that no CV text does. It was then **mutation-checked**:
  a temporary line logging CV text turned it red, and the line was removed.
- **A red that could not test Redis.** The API skeleton failed before it reached the rate limiter,
  so "the suite passes twice in a row" said nothing about leftover limiter state until the real
  handlers existed.
- **An ordering test that depended on random ids.** It created runs within the same second, so
  their order came from UUID generation rather than the rule being tested. It now spaces them a
  second apart.

### When the tooling fails mid-task

One test-writing agent hit a usage limit partway through and stopped. It left behind a file whose
last test built a placeholder and then raised `AssertionError("unused")`, and nine of its ten tests
passed on arrival. Before relaunching, two checks came first: nothing else was still writing to the
file, and what did the file actually prove? The answer was nothing, so it was rewritten from the
spec rather than extended. Half-finished work from a crashed process is untrusted input.

### A trap waiting at the real API

`gemini-2.5-flash` "thinks" before answering by default, and those thinking tokens count against
the output cap of 4,096 tokens. The model could spend the whole budget thinking and return
truncated JSON on every request, and nothing in `make check` would show it. You chose to disable
thinking. The first evaluation against the real API will show whether output quality pays for that.

### The common thread, a fourth time

Day four's bugs needed running to find, day five's needed a review, and day six's needed
measurement. **Day seven's worst bugs lived in the gap between two things that each looked
right:** the spec and the import checker, the API's wiring and the worker's, an exception's message
and the exception object, a passing red and a Redis counter that never moved. Every one was found
the same way: ask what would be observably different if the claim were false, then make that
difference happen.

## Day eight: the front end, the production image, and the first real bill

Day seven ended with a backend nobody could click. This chapter covers the rest of slice 1.3's build:
- the React surface;
- a production image that actually runs a tailoring task;
- four passes of a paid evaluation against the real Gemini API.

The last of those is where the slice stopped being a design and started being a product with an invoice.

### A skeleton that deliberately did nothing

Slice 1.1 taught an awkward lesson. Its React "skeleton" was really a working component, so the red tests
written against it passed on arrival and proved nothing. This time the skeleton (`TailorPanel`, T40) was
**inert on purpose.** It rendered the right roles and placeholder text: no copy, no timer, no branching on
the run's status.

Making it inert exposed three ways a test could pass against it for the wrong reason, and each was named
before the tests were written:

- **The panel takes no props.** It reads the CV list, the posting list and the run list itself. A test
  that stubbed only one endpoint would be testing a crash.
- **The skeleton never fetches a run.** So "no request is sent after the run finishes" is trivially true
  against it. The test has to prove the run *was* fetched before it proves polling stopped.
- **Every negative is vacuous against an empty stub.** "The working state shows no failure language" is
  true of a blank screen. Each negative assertion had to follow a positive one proving that state's own
  copy was on screen.

### Two broken tests that the red hid

The red commit recorded 26 failures, all on the right kind of error. Then the implementation stopped at
21 of 26 passing, and the implementer did the right thing: **it did not edit a test to make it pass.** It
reported two assertions it believed were wrong. Both were, and both had been hidden by the red itself,
because each test had failed earlier, on an element the skeleton didn't render. The broken assertions had
never been executed.

- **An impossible count.** The "no request is sent" tests counted *every* call to
  `/api/tailoring-runs`. That included the list request the panel is *required* to make. No correct
  implementation could satisfy `toBe(0)`.
- **A wait that waited for real time.** The polling tests used `findByText` under fake timers. React
  Testing Library only advances its own internal timer when it detects Jest's fake timers, and it detects
  them through a global `jest` object that Vitest doesn't define. So `findByText` waited on a real
  five-second clock that the fake timers never moved.

Both were fixed by the test author, in a separate commit, **before** the implementation was committed.
That commit's red was re-recorded against the skeleton, with the implementation files set aside, so the
green commit touched no test. A shim that defined a global `jest` would have made the second one pass too.
It was rejected, because it would have changed timer behaviour for the whole suite in order to fix one
file.

**A red test proves the assertions it reached. It says nothing about the ones it never got to.**

### Refresh must not cost money

A user who refreshes mid-run shouldn't pay for a second run. The panel therefore finds the run to watch
from the *list* of runs the server returns, not from anything the browser remembered. After a refresh it
reattaches to the run already in flight.

Two smaller decisions came out of the tests:
- The API client now keeps the extra fields of an error body. That lets "you already have a run in
  progress" link to *that* run.
- Polling stops on a 4xx as well as on a finished run. A purged or expired run would otherwise keep a
  one-second timer spinning until the page closed.

### The production image, and a crash that never exits

The production image built, installed the Gemini SDK from wheels, and ran a tailoring task inside itself.
It ran without a key, so the task spent nothing and recorded `llm_unavailable`, as designed. That part was
routine. The surprise was the startup guard.

Production with no API key refuses to boot, which is correct. But the API runs under
`uvicorn --workers 2`. uvicorn's supervisor respawns a worker whose import crashes, forever. The container
never becomes ready and **never exits**, so Docker's restart policy never fires and nothing looks like a
restart loop. On a real server it shows up as one traceback, logged endlessly. It's in CLAUDE.md now.
**"Refuses to start" and "exits" are different claims.**

### Four prompts and ten imaginary job-seekers

ADR-0004 says prompt quality is a judgement, not a unit test, so `make eval` runs the real API over a
committed corpus of ten CV-and-posting pairs, costs money, and prints its results for a human to read. The
CVs are synthetic, because the repository is public. The postings became synthetic too: a real job ad
isn't personal data, but it is someone else's copyrighted text, and a public repository would publish it
permanently.

**Prompt v1** passed on speed: p50 6.1 s, p95 7.4 s. Reading the output found something no speed check
would.
- **A header that implied a job the candidate never held.** A mid-level nurse's tailored CV swapped the
  candidate's own location for the *target hospital's* name, right under their title. A hiring manager
  reads that as current employment there.

**Prompt v2** fixed the nurse, and broke on the longest CV in the corpus.
- **The timeout.** Pair 09, a 14,700-character CV, timed out on eight attempts out of eight.
- **The measurement.** One call with a longer timeout showed why. It succeeded in 14.5 s with 3,595 output
  tokens, and the "tailored" CV came back at 14,648 characters, against a 14,709-character original.
- **The cause.** The model had read a rule about keeping names and dates "exactly as the CV states them"
  as applying to every bullet. It copied the CV. **Output length drives latency**, at about 250 tokens a
  second, so the bug showed up as a timeout.
- **A misspelled name.** Run 2 also spelled the candidate "TOMAZ" when the CV says "TOMASZ". The eval had
  no check for that. It was found by reading, and a name check was added.

**Prompt v3** fixed the copying, and deleted the candidate's early career.
- **Entries gone.** Three employers and both education entries disappeared, against the prompt's own rule
  to shorten old roles rather than remove them.
- **The name check's first catch.** It spelled the same name "TOMMASZ", the opposite mistake to v2's.
- **A deleted-entry check was added**, so a missing employer is caught by the tool instead of by reading.

**Prompt v4** kept every entry and spelled the name right, and pair 09 timed out again.

Across four prompt versions, the longest input sat right at the edge of the 12-second limit per attempt,
and each prompt change moved it without settling it. You accepted it as a known limitation for very long
CVs, to fix in a later slice, and it's written down rather than averaged away. The eval runner helped keep
that honest: when a run timed out, it **refused to print "within budget"** from the runs that finished,
because a budget measured without its slowest case says nothing.

Three lessons to take from the eval:
- **Every prompt fix moved the failure somewhere new.** A prompt is behaviour, and changing behaviour needs
  the same re-measurement as changing code.
- **Checks were added the moment reading found something.** Name spelling and deleted entries now fail the
  run instead of relying on someone noticing next time.
- **A number that leaves out the failures isn't a result.**

## Day nine: verifying a slice that was already green

By the time `/verify` started, every gate was green: 771 backend tests, run twice in a row, 151 frontend
tests, the build, the types and the import rules. It would have been easy to call the slice done. The
review found a critical privacy leak and a design hole instead, and fixing them properly took two days and
eighteen commits before the closing documentation. Almost nothing in this chapter was a missing test. **It was mostly missing rows in
the failure contract**: failure paths nobody had listed, and a recovery mechanism that turned out not to
exist.

### Checking that a green test would actually go red

The red-first audit came out clean. Every red commit recorded its failure, and no green commit edited a
test. One awkward detail: six of the eight recorded reds were `NotImplementedError` from skeleton
functions, not failed assertions. The SDLC document asks for both, "real signatures with
`NotImplementedError` bodies" and "failing on its assertion", and a test that calls a skeleton can't give
you both.

So the question became a practical one: would these tests catch a real regression? Seven regressions were
put back one at a time, and each guarding test went red:
- the error floor catching `BaseException`;
- a refusal being retried;
- the original error chained into the new one;
- a finished run executed a second time;
- the task returning a value into Redis;
- the rate limiter failing open;
- polling that never stops.

**That is the difference between "the suite is green" and "the suite would notice".** It costs a minute per
guard.

### A privacy setting that covered one layer of three

The reviewer's critical finding was that when a database write fails, SQLAlchemy puts the *values* it tried
to write into the error message. In the worker, that write can be a tailored CV. The error travels through
Celery's log line and into Sentry's report.

`hide_parameters=True` seemed to be the answer, and it was only one layer. With the flag set:
- **SQLAlchemy's own `[parameters: …]` line** disappeared.
- **The database driver's message** still quoted the value.
- **Postgres's own complaint**, `DETAIL: Failing row contains (…)`, still carried the entire row.
- **The chained exception**, which Celery and Sentry both print, still carried all of it.
- **Postgres's server log** held a fourth copy.

A test that only checked `str(exc)` would have passed with the one-flag fix. The tests were strengthened
before the fix went in:
- They check the fully rendered chain as well as the message.
- They require the error to still be the same *type*. Otherwise the router's "database unavailable"
  handler stops catching it, and a 503 silently becomes a 500.
- They require the constraint's name to survive, so the error is still useful to whoever is debugging it.

And the worker test first ran on the *test* suite's own database engine, not the production one. **A test
that bypasses the production configuration can't guard it.** The whole suite now runs on the production
engine factory.

The fix is a listener on each engine the app creates. It rebuilds the error with the driver's text withheld
and the chain cut. It was deliberately **not** registered on SQLAlchemy's global engine class, where it
would have quietly rewritten every database error in every process, including migrations. Postgres now
logs errors tersely, so the server's copy of the row is gone too.

One honest limit was found by testing a claim instead of trusting it. The implementer first wrote that a
refused database password keeps its structured error. It checked, and it doesn't: connection errors never
pass through that listener. That message names the database user, and no uploaded data, so it's
acceptable. The docstring now says what was actually measured.

It's the same trap CLAUDE.md already warned about for Sentry: **several settings that sound like they
protect personal data, and one place that actually decides.**

### "Acks late" doesn't mean "tries again"

The design said that if a worker dies mid-run, Celery redelivers the task, and a redelivery arriving after
five minutes marks the run `abandoned`. Every part of that was checked against the running worker, and none
of it held:
- **A killed worker process** has its message acknowledged, not redelivered.
- **A task that raises** is acknowledged too.
- **A lost main process** gets its message back only after an hour.
- **A quick redelivery would have done nothing anyway.** It finds a run still marked `running`, decides a
  worker is busy with it, and skips it.

Every path left a run spinning forever, with a screen promising "we're still working, don't refresh".
Setting the obvious extra option wouldn't have fixed it, because of that last point.

You chose a **stale-run sweep**: a small job, run by Celery beat every minute, that marks any run stuck in
`running` for more than five minutes as `abandoned`. It's the single recovery path, the same reasoning as
day seven's single retry path. The worker also got a 60-second grace period, so a deploy lets a paid call
finish instead of killing it.

### My own mistake, made in a question

When I offered you that sweep, I said it would "follow the existing guest-purge pattern". There was no
guest purge. The command was a placeholder, beat's schedule was empty, and even a comment in the compose
file claimed beat ran the purge. I found that by checking the code before building on my own claim, and I
asked you again with the facts corrected. You gave the same answer, but this time it was based on a true
description.

**Check the premise of a question before you ask it. If you find it was wrong, ask again rather than build
on agreement to something that wasn't true.** A stale comment is how the wrong premise got in.

### A guard that forced a better commit order

Adding the sweep's database query to the repository interface broke type-checking in six files that
belonged to other roles. The adapter and the in-memory fake therefore got the method first. Python's
structural typing accepts an extra method on a class that implements an interface, so the interface could
gain it last.

That created a second problem. The infrastructure code couldn't be committed while the new tests were
deliberately red. The pre-commit hook's red-test bypass (`TDD_RED=1`) only works when a *test* file is part
of the commit, which is exactly so nobody can use it to commit past a failing suite. My first plan pointed
straight at that bypass. The guard was right, and the plan changed:
- Both roles wrote their changes without committing.
- The combined tree had to pass every check.
- Then the pieces were committed in order.

The result is an infrastructure commit where only the recorded reds fail, and a green commit where
everything passes and no test was touched.

### Three things that only showed up in the running system

- **A partial index needs a query that proves it applies.** The index only covers running rows. Postgres
  uses it only if it can prove the query asks for running rows, and a query with `status = $1` proves
  nothing. With the literal `'running'`, the planner picked the index; with a parameter, it scanned the
  table. Switching back would change the query plan without changing a single result, so the guard is a
  test that inspects the SQL.
- **Two queues, one default key.** Both Celery queues had been declared without a routing key, so both were
  bound to the default one. Today's code happened to be safe. One future routing rule would have delivered
  every tailoring run to both queues: two workers, two paid calls. After the fix, the old binding *stayed in
  Redis*. kombu adds bindings and never removes them, so it took a manual one-line cleanup on the broker.
  **A fix to something that creates lasting state has two halves: the code, and whatever the old code left
  behind.**
- **A health check only sees what it pings.** `/health/ready` checks Celery by pinging it, and only
  workers answer pings. With beat stopped, it reported "ready". It's the day-one lesson again ("a stopped
  worker looks healthy"), one container over. It's written down as a gap for slice 1.6, not built now.

### Idempotency written as a state check

"A redelivered task can't make a second paid call, because a run can only start from `queued`" is true when
the redelivery arrives *after* the first delivery recorded `running`. It's false when two deliveries run at
once. Both read `queued`, and nothing locks the row between reading and writing. The domain object enforces
its rules on the copy it holds; it can't see a second copy of itself in another process. The routing-key
bug would have created exactly that pair. Configuration now prevents the known sources of duplicates, and
the broker's own rare duplicates are recorded as an accepted risk. The second review agreed that naming it is enough for this slice, but only for this slice. Today the
cost is one extra paid call. In 1.4 a user can edit a draft, and a second worker's draft would silently
overwrite their edits. So it must be closed before editing ships. The planned fix is a version column that
turns a stale write into a domain error, caught before the model is paid.

### The second review, and a test that broke everyone else's tests

The second review found no critical issue. The one blocker was a test. The migration test downgraded the
database, checked that the index was gone, and restored it in a `finally`. If the regression it guarded
actually happened, the downgrade would commit with the index still there. The restore would then fail on a
duplicate index and leave the version table behind the real schema. **Every later test run would break at
setup**, and the error from the restore would hide the assertion that explained why. The test author had
already hit exactly this while proving the test worked, and had fixed the database by hand without
noticing it was a design flaw rather than bad luck.

**A test that damages shared state when it fails turns one red into a broken suite for everyone who comes
after.** It is the same rule as clearing Redis between tests, applied to the schema. The test also
downgraded by "one step back", which will point at the wrong migration the moment 1.4 adds its own.

Two more findings from that round are worth keeping:

- **A rule enforced by a comment is a suggestion.** The sweep's five-minute window must stay above the
  worker's three-minute hard limit, or it will mark live calls abandoned. That was written down, and the
  test only checked the defaults. But the window is an environment variable that an operator would shorten
  "for faster recovery". It is now a startup refusal, which is the pattern the codebase already uses for a
  missing API key.
- **Proving a test can also cause the problem it tests for.** To show the routing-key test catches a
  regression, the test author broke the queue declaration locally. The dev worker auto-reloaded on that
  edit, re-declared the queues, and put the stale binding back into Redis, where it outlived the revert.
  It was the second time in one day, and this time the cause was an agent that knew about the trap.
  **Mutation-testing something that writes durable state needs its own cleanup step.**

### Smaller lessons, still worth keeping

- **The Gemini SDK's logger decision was reversed.** Day seven measured the SDK's ordinary call path and
  found it safe. But a debug log line on its streaming path writes raw response text, so the SDK's loggers
  are silenced anyway. Silencing them also removed the only log records the logging test could see, so that
  test had to be rewritten around what it could still observe.
- **The Gemini client was never closed.** Its cleanup ran on an event loop that had already shut down.
  Closing it also needed two calls, because the client holds two HTTP clients and each close method closes
  only one.
- **A "not queued" error handler listed only database errors**, so any other failure became a 500 instead
  of the contract's 503. A handler that lists the errors it expects needs a catch-all under it.
- **A handler that could never run was left out.** The brief asked for one, and the implementer showed the
  situation it handled could never happen. An error handler for an impossible case reads like safety and
  is dead code.
- **Alarming logs can be harmless, but prove it.** Nine tracebacks and two "Fatal Python error" lines
  turned out to be the development auto-reloader interrupting its own restarts. Two of them matched a file
  save to the millisecond. It was the matching timestamps that settled it.
- **A test agent stopped mid-task**, twice this slice. Both times the working tree was inspected before
  the agent resumed.

### The third review, and a wrong sentence in the most trusted place

The third and last review passed, with no critical or major issue. Its two minor findings are worth
keeping, less for their size than for how they were framed.

**The first was a question it was asked to grade.** The new stale-window refusal runs when the API imports
the Celery app. So a bad value leaves the API respawning forever instead of exiting. The reviewer accepted
that, for two reasons: it fails closed, and it has exactly the shape of the API-key guard that was already
accepted. But the acceptance came **only once an owner and a trigger were written down somewhere that
lasts.** "Carried to the final report" is not a plan. The item now names `devops`, with the trigger "before
the deploy's SSH secrets are set", recorded in CLAUDE.md, because the roadmap isn't tracked in git.

**The second was four sentences that still said a dead worker's task comes back.** One of them was in the
docstring for `mark_started`, on the domain object itself. A future implementer trusts that place most, so
that's where a wrong explanation does the most damage. The next slice's editing logic would have been built
on the exact misunderstanding that caused the stuck runs. **A false comment does harm in proportion to how
much it's believed.**

### The common thread, a fifth time

Day seven's worst bugs lived between two things that each looked right. Day nine's lived between **a
setting and what it actually does**:
- `hide_parameters` and the driver's own error message;
- `acks_late` and "try again";
- a routing-key default and a second queue;
- a readiness check and a scheduler it never pings.

Each was found the same way as before: say what would be observably different if the reassuring name were
true, then go and look.

## Days ten to twelve: the editor, and the number that belongs to nobody

Slice 1.4 is the first one where a stranger *writes* into a row that holds their own CV. Until now a CV
arrived once, as a file, and left once, to Google. From here on it arrives every 1.5 seconds of typing, as a
JSON body, and lands in a second pair of columns beside the model's draft. The slice took three days,
fifty-odd commits, and three rounds of review before it passed — and the two hardest bugs were in code
that every gate had already called green.

### Who owns the version number?

The design question that shaped everything else was small and looked administrative: when the editor
says "this edit applies to version 7", who decided it was 7?

SQLAlchemy will happily do it for you. Declare `version_id_col` and the mapper increments the column on
every `UPDATE` and adds `WHERE version = :loaded` to the statement; two writers who both loaded 7 can't
both win. It costs nothing and it closes the concurrent-duplicate-delivery hole slice 1.3 left open.
But the number then lives in the ORM, invisible to the domain: a pure unit test can't assert a version,
and the editor's "you are stale" rule has to be checked in a use case against a value the aggregate
never set.

So the aggregate owns it. `TailoringRun` increments `_version` in every named transition —
`mark_started`, `mark_succeeded`, `mark_failed`, and now `revise_cv` and `revise_cover_letter` — and
the mapping declares `version_id_generator=False`, which tells SQLAlchemy "check it, don't touch it".
The domain has a rule about a fact the domain owns; the database enforces it where two processes meet.

**The catch, and it is the whole reason there is a table-driven test:** with the generator off,
SQLAlchemy's check only *detects* a race the application bumped past. Two writers who both leave the
column at 5 both match `WHERE version = 5`. A transition that forgets its `+= 1` has no concurrency
protection at all, and nothing will tell you. That is why "every transition increments by exactly one"
is an invariant with a walk over every legal path rather than a convention in a docstring.

A quieter lesson came with it. The domain change and the mapping change could not be committed
separately: a run loaded from Postgres without a mapped `_version` has nothing to bump, so sixteen
integration tests failed the moment the domain was right and before the mapping was. The honest fix was
one commit for both, saying why — not a `getattr(self, "_version", 1)` fallback, which would have hidden
exactly the missing-mapping hole the invariant exists to expose.

### No HTML, anywhere

The editor is TipTap, which is ProseMirror, which renders a document from a *node tree*, not from a
string. That fact decided the format and the security argument at once.

The stored format is Markdown — the model already writes it, the value objects already validate it, and
1.5's four export formats all derive from it on the server. The client parses Markdown into tokens with
`markdown-it` running `html: false`, so a `<script>` in the model's output is a *text token* by
construction, and `prosemirror-markdown` turns tokens into nodes. No HTML string ever exists on the
client. There is nothing for DOMPurify to purify, and a test greps the source for
`dangerouslySetInnerHTML` to make sure it stays that way.

The argument has exactly one hole, and writing it down was more useful than closing it quietly: a mark
with a URL-valued attribute. `href` on a link is the whole residual risk. It's closed by an allow-list of
three schemes — `http`, `https`, `mailto` — applied at **both entrances**: `markdown-it`'s
`validateLink` on the way in, and the Link mark's `isAllowedUri` on the way out. That second one was
measured rather than assumed, and the measurement mattered: TipTap's `protocols` option is *additive*,
so configuring it alone still let `ftp:`, `tel:`, protocol-relative and bare-host hrefs through. The
schema module now carries an inventory of every attribute it can hold (the Link mark has five, not one)
so the next person who adds an Image node finds the argument they are about to break.

### A serializer and a parser are one grammar only if…

The first review found the bug that most embarrasses me, because every test was green and the fixture
that would have caught it was the most ordinary line in any CV: `[https://github.com/jane](https://github.com/jane)`.

`prosemirror-markdown`'s default link serializer has a shortcut: when a link's text equals its `href`,
it writes CommonMark's autolink form, `<https://github.com/jane>`. Perfectly legal. But our tokenizer
runs a grammar-only preset with `autolink` switched off — ADR-0015 names `[text](href)` as the one link
form — so on the very next parse those angle brackets were read as plain text. The link mark was gone and
the brackets were in the CV. Worse, 1.5's Python renderer *would* have rendered `<url>` as a link, so the
PDF and the editor would have disagreed about the same row.

AC-29's stability test — `serialize(parse(serialize(parse(md)))) === serialize(parse(md))` — passed
throughout, because it compares *strings*, and the string was stable. The loss was structural. The fix was
a link serializer that always writes `[text](href)`; the test that pins it collects the set of link-mark
hrefs from the parsed document before and after a round trip and compares those. **A serializer and a
parser are one grammar only if the parser accepts everything the serializer can emit.** Round-trip
tests should compare the thing you care about, and here it was marks, not characters.

### A rollback is a statement about the whole session

The second review finding was in the beat sweep, and its diagnosis was wrong twice before it was right.

E-20 promised that when the sweep's write loses a race — a worker decided the run between the sweep's
read and its `save` — the sweep counts a conflict and moves on to the next run. Against the in-memory fake
it did. Against Postgres, the tick crashed on the *next* run with `MissingGreenlet`: the async session was
being asked to lazy-load an attribute outside the one place it is allowed to.

The obvious diagnosis: the committing repository calls `rollback()` on a conflict, and a rollback expires
every instance in the identity map — including the remaining candidates the loop still holds. The obvious
fix: wrap each write in a SAVEPOINT with `begin_nested()`, whose rollback expires only what was modified
inside it. Both obvious, both half wrong, and the agent who implemented it measured instead of trusting
me:

- The expiry never came from our `rollback()`. A *failed flush* rolls itself back to the nearest
  transaction boundary before any `except` runs, and at the root boundary that means `dirty_only=False`
  — the whole map. Our `rollback()` was closing a transaction that was already dead.
- `begin_nested()` **flushes on entry**. The port contract is "mutate, then save", so the aggregate was
  already dirty when the wrapper opened the SAVEPOINT — and got flushed *outside* it, un-translated,
  expiring everything again.

The shape that works is `expunge` the aggregate, open the SAVEPOINT, let the inner repository `add` and
flush inside it, commit. On a refused `UPDATE` only that run is expired; the other candidates keep their
state; the tick finishes. The same family of fact had already bitten once that week: the repository read
`run.id` *after* the failed flush to log it, and the read raised `PendingRollbackError` — a
`SQLAlchemyError` — so the 409 the client was owed came out as a 503. Read what you need into a local
before the flush. A rollback, however it happens, is about the session, not the one object that failed.

### A "keep mine" nobody clicked

The third round's finding was mine to own: it came out of a fix from the second.

The autosave hook uses a TanStack Query mutation `scope` so that the two documents of a run save in
order — the letter's `PUT` waits for the CV's and carries the version it returned. That queue was also
letting a *second* save of the *same* document line up behind an in-flight one. Now trace a 409: the
first `PUT` is refused, the handler refetches the run to compare — which puts the fresh version in the
cache — and TanStack runs the queued mutation, whose `mutationFn` reads the version at execution time.
Fresh version, current text, 200. The other writer's edit is overwritten and the two-choice notice AC-33
promised never appears. Optimistic versioning exists so that two writers never silently overwrite each
other; a queue that re-reads the version and retries is *Keep my version* without the click.

The fix removes the queue for a single document: at most one `PUT` in flight per document, and a change
during a save records a *wish* — the variables to send — that the landing decides. A 200 sends the wish
once, if the text still differs from what was saved. A 409 drops it and waits for a human. The scope
stays, because two *documents* should still take turns.

### The type gate that checked nothing

The editor skeleton surfaced something older than the slice. `web/tsconfig.json` is solution-style —
`files: []` plus two project references — and a bare `tsc --noEmit` on that file compiles **zero
files** and exits 0. The Makefile, the `build` script and CI all ran that form. For four slices, the
"types" gate was `vite build`'s transpile and nothing more; it passed a deliberate
`const x: number = "nope"`.

`tsc -b --noEmit` follows the references. Turning it on found seven real errors, one of which was a
version mismatch — vitest 2 nesting its own vite 5 beside the project's vite 6, so the config file's
`test` block never type-checked — fixed by moving to a vitest that peers on vite 6. Slice 1.3's chapter
already had the sentence for this: a gate that checks a different thing than it claims is worse than no
gate, because it also supplies confidence.

### The tests that were wrong, and the discipline that let them be

Every GREEN in this slice exposed a few test defects, and the number is worth saying out loud: about
twenty across the slice. None was a test that had been captured from running the code — the rule the
codebase most fears. They were the ordinary ways a test written *before* the implementation misjudges the
world:

- the in-memory fake hands `get()` the same object the use case just mutated, so "the row is unchanged"
  is unsatisfiable against a fake with no row apart from the object;
- Testing Library computes an accessible name via a spec whose step 2A returns `""` for anything
  `hidden`, so "find the hidden tab panel by name" can never match;
- SQLAlchemy's identity map is *weakly* referencing: a "pre-loaded stale copy" nobody holds is collected
  at once, and the race you meant to stage never happens;
- a `setTimeout` armed under real timers is invisible to a fake clock installed afterwards;
- one test set `document.visibilityState = 'hidden'` and never restored it, and TanStack's focus manager
  paused every later retry in the file — the tests didn't fail, they hung;
- `userEvent.type` clicks a ProseMirror editor before typing, and where the caret lands depends on
  `elementFromPoint`, which jsdom doesn't have, so one run in three the keystrokes landed at the start.

What made this survivable was the rule that a test may not be edited in the commit that makes it pass.
Each fix went into its own test-only commit, *before* the GREEN, saying which of the spec and the test
had won and why. The history shows every RED, every fix, every GREEN, and a reviewer can check in one
`git log` that no implementer ever touched a test to make it green. Five such commits for twenty
defects, and by the end they read as the slice's most honest paragraphs.

### Working with agents that get cut off

Four times during the slice a sub-agent died mid-task on a session limit. Two of them had written nothing;
two had written *most* of their files and run *none* of the gates. The second kind is the dangerous one:
the editor skeleton's docstrings said "measured" about things nobody had run. The successor agent was
told to treat every claim in the inherited files as unverified, and it found one that was wrong
(the `protocols` option being additive). The habit generalises past agents: a file that says "measured"
is a claim about a past that may not have happened.

One agent's probe script did real harm. It called `get_settings()` under `APP_ENV=test`, got the *dev*
database URL — only the test fixtures override that, not the settings object — and its cleanup deleted
every tailoring run and guest session on dev, taking every CV and posting with it through the cascades.
Nothing on the uploads volume, nothing in the test database, nothing anyone could not re-seed. But the
lesson goes into CLAUDE.md as a footgun: anything that deletes must name the test URL explicitly and
assert `_test` is in it before its first statement.

### Smaller lessons, still worth keeping

- **TipTap in jsdom** needs `Range.prototype.getClientRects`, `getBoundingClientRect` and
  `document.elementFromPoint` polyfilled — measured by mounting one and reading the stack traces. And
  `useEditor`'s `setOptions` deliberately preserves `editable`, so read-only-on-401 must be
  `editor.setEditable(false)` from an effect, not a prop.
- **A `<Link>` outside a router throws; an `Outlet` outside a router renders nothing.** Which is why the
  layout component may keep its header but not a link home.
- **`beforeunload` guards the browser's exits. React Router's exits need `useBlocker`.** Two doors, one
  lock — and a data router consults exactly one blocker, so the lock lives in the component that owns
  both editors.
- **TanStack Query collects a query with no observer and `gcTime: 0` on the next tick.** A hook that
  wants to *read* a key it does not render must hold a disabled observer on it.
- **Tailwind v4 scans comments.** A doc comment containing the bare word "transition" shipped an unused
  CSS rule. Compare the CSS asset hash against `main` when you only changed prose.
- **A skeleton must be inert.** The frontend skeletons render roles and nothing else — no copy, no
  branching, no default — because a skeleton that already works makes the RED pass on arrival, which
  the previous two slices had each learned once.

### The fourth round: replacing six refs with one machine

Three rounds without a pass is the process's own signal that the *design* is wrong, and the owner
took it that way. The autosave hook had grown six refs — the rendered state's mirror, "in flight",
"resend wanted", "last saved", "last sent", the timer — plus a reducer, and every round had found a new
gap between two of them. Option A was to pin the remaining gaps with two red tests and three small
fixes. Option B was to throw the refs away.

B won, and the shape it produced is the one I'd teach from now. `autosaveMachine.ts` is a pure module
with no React, no TanStack and no HTTP in it: ten states (`idle`, `debouncing`, `inFlight`,
`resolvingConflict`, `conflict`, `paused`, `failed`, `invalid`, `expired`, and `leaving` for a
document whose component is gone while a save is still on the wire), eleven events, four effects
(`send`, `refetch`, `armTimer`, `clearTimer`), and one function: `step(machine, event) → { next,
effects }`. The hook holds the machine in a single ref, performs the effects, and *derives* the
rendered save state with `useSyncExternalStore` — state that is read from timers, promises and DOM
events is outside React by definition, and a `useReducer` mirror is exactly the copy whose lag round 3
had found.

Each of round 3's findings became a single transition you can point at: resend-on-200 is `inFlight +
landed200` (send once, iff the text still differs from what was just saved); the unmount rule is
`inFlight + unmount → leaving{wish}` with the text captured while the editor still exists, so nothing
ever reads a destroyed one — and the `leaving` rows of the table pass a `textNow` that *throws*, so that
sentence is an assertion; the 409 window is gone because the guard reads the machine, never a
committed React state. The reviewer ran the mutations: delete the resend transition and two tests go red;
measure "still differs" against the wrong baseline and seven do. A 10 × 11 sweep asserts that every
(state, event) pair the table doesn't name lands on the default branch — the same machine, no effects —
so a new state or event without a row fails on arrival.

The old 76-row reducer table was retired rather than edited. Two of its rules *were* the removed
design — "a second send is permitted while one is in flight" and "the reducer adopts whatever it is
told" — and a table certifying a deleted design is coverage the assertion cannot deliver. A test that
must change because the design changed is not a test edited to pass; the commit says which is which.

The fourth round passed, with four small findings that took an hour: the hook's `dispatch` had been a
`useMemo`, which React may discard — so the store created once now owns `dispatch`, the effects and the
timer, and the unmount effect can fire for exactly one reason; the docblocks that still described the
six refs were re-pointed at transitions; a StrictMode test pins the dev-only mount-unmount-mount
rehearsal that would otherwise be the first thing a newcomer breaks.

### The common thread, a sixth time

Day nine's bugs lived between a setting and what it actually does. This slice's lived between **a
mechanism and the moment it fires**: a serializer's shortcut and the parser that never learned it; a
flush that rolls itself back before your `except`; a `begin_nested()` that flushes before it opens; a
queued mutation that runs after the refetch that was meant to stop it; a timer armed before the fake
clock existed; a `useMemo` that may forget. In every case the code was reasonable and the *order* was
wrong, and in every case the fix began with someone refusing to accept my diagnosis until they had
measured it. And when three rounds of that were not enough, the answer was not a seventh ref — it was a
table you can read.

## Days thirteen and fourteen: the file at the end

Four slices in, TailorCraft could take your CV, read a job posting, rewrite both documents with a
model, and let you fix the sentences it got wrong. What it could not do was give them to you. The
whole product ended at a text box.

Slice 1.5 is the one that ends with a file in your downloads folder — four formats, two of them
cheap enough to make while you wait and two expensive enough to leave the request. It is also the
slice where a stranger's employment history becomes **HTML** for the first time, which is the one
form a browser would execute, and then becomes a **PDF on a disk**, which is the most portable and
least deletable shape it has ever had. Everything careful about this slice follows from those two
sentences.

Here is what it cost to get right.

### The pipeline that was right on paper

The plan for the rendering pipeline was, I think, genuinely good. One parse, one normalization, three
walkers:

```
Markdown → tokens (html=False) → normalize to the grammar ─┬─→ plain text
                                                            ├─→ DOCX
                                                            └─→ HTML → nh3 → WeasyPrint → PDF
```

with four obligations, each owned by exactly one stage: the parser refuses raw HTML and any link
scheme outside `http`/`https`/`mailto`; the normalization clamps `####` to `###` and turns everything
outside the grammar into text; `nh3` is a **second** lock that exists for the day someone enables a
rule; and WeasyPrint gets a `url_fetcher` that refuses every URL, so there is no outbound request to
guard at all.

Three of those four were wrong in some way that only running them revealed. Not the *ideas* — the
ideas held. The mechanisms.

### A fetcher that the library refused on its way to refusing

This is the one I want you to remember.

WeasyPrint reaches the network in exactly one place: a callable you hand it, which it calls for every
image, stylesheet, `@import` and `@font-face` in the document. Ours raises on every input. That is
the whole of "this slice makes no outbound HTTP request", and the test for it asserts that **no
socket is opened** — not that a fetch failed.

It didn't work. Feeding it hostile HTML with a remote `<img>` produced this:

```
AttributeError: 'function' object has no attribute '_fail_on_errors'
  at weasyprint/urls.py:428, inside the library's own except handler
```

WeasyPrint's `fetch` calls your fetcher, catches the exception, and then reads
`url_fetcher._fail_on_errors` off it to decide whether this is a per-resource problem (log it, render
the page without that image) or a fatal one (abandon the document). A **function** has no such
attribute. So our refusal came back out as an `AttributeError` raised *inside the library's error
handling*, the render died completely, and the adapter's `except Exception` floor would have
faithfully recorded `render_error` while saying nothing whatsoever about why.

The fix is a five-line wrapper class carrying `_fail_on_errors = False`, which every fetcher passes
through — a wrapper rather than an attribute bolted onto our function, because the *test's* recording
fetcher has no more `_fail_on_errors` than a bare function does.

Two lessons, and the second is the bigger one.

The small one: a vendor's documented extension point can have an undocumented shape requirement, and
the only way to find out is to run it against the version you actually installed.

The big one: **the floor that makes your port's promise true is also the thing that can hide why it
was needed.** An `except Exception` that translates everything into `DocumentRenderError` is correct
— it is what lets the use case promise "every failure is a recorded state" — and it is exactly why
this bug would have shipped as "some PDFs mysteriously fail" rather than as a traceback. It was
caught because the task list said to *measure* AC-30(c) with a patched socket, not to reason about it.

### An exception that is not an Exception

While measuring what WeasyPrint can throw — by walking every module in the installed package and
collecting every `BaseException` subclass, rather than reading the changelog —
`weasyprint.urls.FatalURLFetchingError` turned out to inherit from **`BaseException`**, not
`Exception`.

Which means it goes straight through `except Exception`. Every floor in this codebase is written as
`except Exception` on purpose, because `except BaseException` would swallow `asyncio.CancelledError`
and break cancellation. So the floor is right and there was a hole in it, and the only fix is to catch
that class **by name** in the specific-translation tuple above the floor.

You cannot enumerate what a library can raise by intuition. You can enumerate it by import and
`issubclass`, in about fifteen lines, and then you know.

### The sanitizer that ate its own document

The plan said, in one line: `sanitize_html(render_html(tokens, document))` → `render_pdf(html)`.

Run it and the PDF has a stray line reading **"Tailored CV"** above the candidate's name, in every
single document.

`render_html` wraps the body in `<!doctype html><html><head><title>Tailored CV</title>…`. `nh3`'s
allow-list contains the eleven tags the *document grammar* can produce — `p`, `h1`, `ul`, `a` and so
on. It does not contain `html`, `head`, `title` or `body`, and it correctly should not: an allow-list
for a document grammar has no business listing the page chrome. So `nh3` did precisely its job,
deleted the shell, and **kept the title's text content** as a bare text node at the top of the
fragment.

The fix is to sanitize the fragment and *then* wrap it — which is what the plan's own next sentence
said ("the shell around **the sanitized fragment**"), one line later, contradicting the code sample
above it. I corrected the plan in place so the next reader can't re-derive the broken order.

The lesson is about composition. Both functions were correct. The pipeline was wrong because
"sanitize" and "wrap in a document" are not commutative, and nothing in either signature says so.

### A link that will not lose only its href

The spec says: a link whose scheme we refuse should render as **its text alone, no URL**. Reasonable.

markdown-it does not do that. When `validateLink` returns `False`, the whole link rule *fails* and
the author's literal characters stay in the stream as one text token: `[click me](javascript:alert(1))`,
brackets and payload included. There is no configuration that yields a `link_open` without an
attribute.

So the PDF would spell out the attacker's `javascript:` URL as visible text. Not executable — nothing
in this pipeline can execute — but it is the payload, printed, in the document the user is about to
email to an employer.

We now recover the label from the residue with a small helper that both human-facing walkers call.
I want to flag it honestly, because it is the least comfortable thing in the slice: it is a text
transformation applied to every text token, it has a known false positive (a legitimate `[1](note)`
citation becomes `1`), and it exists because an acceptance criterion pinned the strict
`validateLink` and a failure contract row pinned the output, and those two together left no other
door. It is on the `/verify` list for exactly that reason.

### Five tests that could not build themselves

The most productive thing that happened in this slice happened three times: **an implementer stopped
and said "I think these tests are wrong" instead of making them pass.**

The first time, two application tests. One started an export job *one second before it was requested*,
which the aggregate forbids — it could never have passed against a correct implementation. The other
asserted that a job whose save was refused still reads `rendering`, which is a property of the
in-memory fake (it hands back the very object the sweep mutated), not of the code. The sibling test
one context over already had the right assertion **and a comment warning about this exact trap**; it
just hadn't been carried across.

The second time, two API tests — one measuring event-loop liveness through 20 concurrent HTTP requests
that all share a single database session (SQLAlchemy refuses concurrent operations on one; it answered
503, not 200, and no implementation could have changed that), and one privacy test that uploaded six
base CVs under a session capped at five, so it died in its own setup before reaching the assertions it
existed for.

The third time, six frontend tests. Five asserted `Download PDF · 84 KB` for a file whose fixture said
**one byte**. The tell was beautiful: `findByText(..., { exact: false })` means *the element's text
contains the matcher*, so the matcher can never be longer than the text — and a sixth test in the same
block used 86,016 bytes with the *identical* assertion and passed. Same assertion, discriminating
correctly. That is what makes it five copy-paste fixtures and not a disagreement about formatting.

Every one of those was fixed by the person who wrote the tests, never by bending the code to match.
That distinction is the entire value of the arrangement.

And one of them taught something extra: fixing the length-bound fixture let the loop-liveness test run
past its first iteration **for the first time**, which revealed a *second* bug in the same test. The
first crash had been hiding it. Bugs queue.

### The literal that was three

A test asserted that a newly created export job carries `run_version == 1`.

A tailoring run is at version **3** when it succeeds — `request`, `mark_started`, `mark_succeeded`,
one bump each — and 4 after a revision. So the assertion was simply wrong.

The interesting part is what *not* to do about it. Writing `3` and `4` would pass, today, and would
bake the tailoring run's internal transition count into an **export** test, so that changing the run's
lifecycle would break an export test for a reason having nothing to do with exports. The acceptance
criterion doesn't say "3". It says `run_version` **equals the run's `version`** — a *relationship*.
So the test now reads the run's version back and compares, and for the second case asserts the new
job's version is *greater than* the old one's, which is that row's actual claim ("the run's version
moved").

When a test has a wrong literal in it, the fix is usually not the right literal.

### The measurement that overruled the acceptance criterion

AC-11 asked for two things from the loop-liveness check: a p50 under 5 ms, and no stall over 50 ms.

Measured over twelve trials, max latency reliably exceeded 50 ms — up to 97 ms — while p50 stayed
under 0.15 ms every single time. That is not the event loop being blocked. That is the GIL, with
twenty genuinely concurrent CPU-bound Python threads taking turns and occasionally starving any other
thread, including the loop's own. The precedent test one slice back, measuring the same property for
the posting fetcher, asserts **p50 and never max** — and its docstring names this exact mechanism.

So the clause was struck, on measurement, and written up in the spec with the numbers. p50 is the
clause that discriminates the thing the criterion exists to catch: a synchronous parse *on* the loop
destroys p50, not merely the tail.

I want to be careful about how this reads, because "the measurement overruled the spec" is also what
it sounds like when someone quietly lowers a bar they couldn't clear. The difference is that the
residual gap is written down: max-latency behaviour under true 20-way concurrent render load is now
**unverified by any test**, and that is inherent to CPython at this shape rather than something a
cleverer test would reach. A struck criterion with its replacement measured and its gap named is a
decision. A struck criterion with neither is a lie that ships.

### A binding set named after the wrong thing

Slice 1.3 poisoned the dev broker by declaring two Celery queues without routing keys, so both bound
under the key `celery` — one publish would have reached both queues, meaning two deliveries of one run
and two paid model calls. The lesson went into CLAUDE.md, and this slice adds a third queue, so it was
read very carefully.

And it was wrong. Not the warning — the *check*. It said to confirm one member in each of
`_kombu.binding.celery`, `.tailoring` and `.export`. But kombu names a binding set after the
**exchange**, not the queue, and all three queues sit on the one default exchange. The healthy state
is **three members in the single set `_kombu.binding.celery`**, and the other two keys do not exist
at all. Following the documented check would have read a perfectly healthy broker as broken.

There was a second trap underneath: the broker is Redis database **1**, and `redis-cli` without
`-n 1` reads database 0, where all three sets are empty — which looks like a clean broker for entirely
the wrong reason.

Both are fixed in CLAUDE.md now. The uncomfortable part is that this was in the most-trusted document
in the repository, written down *because* it had already cost a day, and it was still wrong in a way
that would have cost another one. A footgun note is code; it needs the same scepticism.

### The directory that did not exist

The production image built. The PDF rendered inside it with a real embedded font. The API came up
healthy. And `beat` crash-looped.

```
PermissionError: [Errno 13] Permission denied: '/var/lib/tailorcraft/state/celerybeat-schedule'
```

The Dockerfile pre-creates `/var/lib/tailorcraft/uploads`. It never created
`/var/lib/tailorcraft/state`, which is where the beat schedule volume mounts. When Docker mounts an
**empty** named volume over a path that doesn't exist in the image, it creates the mount point
**root-owned** — and `beat` runs as an unprivileged user in the production target, though not in dev,
because the dev override never switches off root.

So: invisible in development, by construction, for the entire slice. Only a real production image with
real volumes finds it. That is the whole argument for the verification step that builds the shipped
image and starts all three containers, rather than trusting that a green test suite means a green
deploy.

### Working with agents that get cut off, again

Three delegated agents died mid-task on rate limits this slice. Two had written nothing; one had
written 96 KB of tests and died **before running them**.

That last one mattered. Committing what was on disk would have looked entirely reasonable — 84 tests,
collecting cleanly. Running them first showed 58 failures, of which roughly ten were not red tests at
all but **fixtures that could not construct themselves**, and those would have survived the GREEN step
and looked like implementation bugs a day later.

The habit that saved it is small: when you inherit half-finished work, don't ask "does this look
done". Run it, and then *classify every failure*. A census of failure reasons — how many are
`NotImplementedError` from the right function, how many are route-missing 404s, how many are fixture
exceptions — takes two minutes and is the difference between a red commit that means something and one
that poisons the next step.

### Smaller lessons, still worth keeping

- **`exact: false` means "contains".** So the matcher can never be longer than the text, and a test
  written that way will pass for text you did not intend and fail in a way that looks like a
  formatting bug.
- **An `await` that resolves on the wrong render is not a wait.** The empty-state test awaited a
  button that the *loading* state also renders — disabled — so it proceeded immediately and asserted
  against a pending DOM.
- **A migration's DDL is not inside your test's rollback.** Mutation-testing a migration test
  committed real DDL to the test database and left it two indexes short. Worse: the established
  recovery path checks only that the table exists, so it cannot notice a schema missing indexes or
  constraints.
- **XML forbids control characters, and Markdown doesn't.** `\x0b` survives the parse and makes
  `python-docx` raise, so a document that renders fine as PDF, text and Markdown fails *permanently*
  as DOCX. Now stripped, in the one walker that needs it.
- **PDF fonts are Type0 composite**, so a page's `/Font` has no direct `/FontDescriptor` — it carries
  `/DescendantFonts`, with the descriptor and `/FontFile2` a level down. The first version of the
  font-embed assertion checked the wrong level and failed against a perfectly good PDF.
- **Two contradictory numbers in one spec table.** The column was specified as `VARCHAR(64)` and, one
  line later, as reusing a type decorator whose implementation is `String(512)`. Reuse won — a second
  decorator differing only in a length is how one storage grammar ends up validated by two rules.
- **A vendor logger is a leak a clean adapter cannot prevent.** WeasyPrint logs the URL it failed to
  fetch, at ERROR — which is the user's own typed URL, the thing our adapter deliberately logs only
  the *scheme* of. Silencing it is a privacy control, not noise control, and the comment says so.
  (`fontTools` is the noise: 327 records per render.)
- **A render prop beats a callback when the state is computed during render.** Lifting the editor's
  save state to the page through `useState` + `useEffect` would have been a second copy, one render
  behind — stale in exactly the frame where the user just typed, which is the only frame the gate
  cares about.

### The common thread, a seventh time

Day nine's bugs lived between a setting and what it does. Slice 1.4's lived between a mechanism and the
moment it fires.

This slice's lived between **a contract and the thing on the other side of it**. A fetcher we handed to
a library that required a shape we didn't know about. A sanitizer given a document its allow-list was
never written for. A parser asked for an output it has no way to produce. A binding check written
against the wrong noun. A volume mounted over a directory that wasn't there. A test asserting the
behaviour of its own fake.

In every one, both halves were individually defensible and the *interface between them* was assumed
rather than measured. And in every one, the thing that found it was the same: someone ran it against
the real version, on the real image, and read what actually came back — instead of reasoning about what
should have.

## Day fifteen: the verify that found three

Slice 1.5 arrived at `/verify` looking finished. 1293 backend tests and 486 frontend tests, green
twice in a row. Every budget measured rather than assumed. A production image built and started.
Three ADRs. A failure contract with fifty-seven numbered rows, each one traced to a test.

`/verify` found three MAJOR defects. All three were in code that the green suite was perfectly happy
with, and all three were the kind a user would have met in the first week.

That is not a criticism of the suite. It is the single most useful thing this file records about
testing: **a test suite tells you the code does what the tests say. It cannot tell you the tests say
the right thing.** Everything below is a variation on that.

### The helper that deleted the salary range

The worst one first. A CV containing

> Negotiated salary range [100k](150k) for the team

came out of every PDF, every DOCX and every plain-text export as

> Negotiated salary range 100k

The range was gone. Silently, from the one document this product exists to produce, for a user who
is unemployed and about to attach it to an application.

The mechanism is almost elegant in how reasonable each half looks. Markdown-it refuses `150k` as a
link destination — correctly, it is not a URL — and, as day fourteen's notes already record, a
refused link does not come back as a link without an `href`. It comes back as the author's literal
characters: one text token reading `[100k](150k)`. So a helper walks every text token and recovers
the label, because the failure contract says a refused URL must never be shown to the reader.

To decide "was this refused?", the helper asked `_allow_three_schemes` — the same predicate the
parser uses. And that is the bug, in one line: **those two functions are asking different
questions.** The parser's question is *"may this become an `href`?"*, and for `150k` the answer is
rightly no. The helper's question is *"was this ever a URL, such that showing it would show a URL?"*
— and for `150k` the answer is also no, but it is a *different* no, and collapsing them deletes the
user's text.

The fix is a second predicate, `_is_refused_url`, that says yes only for a destination that *tried*
to be a URL: a scheme outside the allow-list, or a protocol-relative `//host`. No scheme at all
means it was never a URL attempt, and the literal survives byte for byte.

**Why no test caught it.** There is a fixture corpus, and the acceptance criterion requires 100 % of
it to render to all four formats. It scored 28/28. But the corpus's only link fixtures were an
accepted `https://` one and a `javascript:` one — and for `javascript:` stripping the destination is
*correct*. The corpus contained no `[label](plain-word)` anywhere, so a 100 % pass rate was measuring
a document shape the bug could not affect. It does now, mirrored into both the Python fixtures and
the TypeScript ones, so this class of document travels with the project.

### A one-character scheme is a drive letter

The fix introduced a smaller version of itself, which is worth recording because the reasoning that
settled it is reusable.

`urlsplit("C:/Users/me/cv.docx").scheme` is `"c"`. So a Windows path in a CV has a scheme, takes the
refused branch, and `[docs](C:/Users/me/cv.docx)` still collapses to `docs`.

The first instinct — mine, and the implementing agent's — was to accept it: a scheme-shaped
destination *is* a URL attempt by the grammar's own rule, and a Windows path in a CV is rare. The
reviewer argued the other way and was right, on a principle worth keeping:

**The harm is asymmetric, and only one side of it is recoverable.** Over-stripping deletes the
author's characters and the reader never learns anything was removed. Under-stripping merely shows
inert text — by the time this helper runs the link is already a text token, there is no `href`, and
nothing can be clicked, so the security cost of being wrong in *that* direction is exactly zero.

When a heuristic must be wrong somewhere, put its error on the side that shows too much rather than
the side that silently deletes. No IANA-registered scheme is one character, so the guard is
`len(scheme) > 1`, and the case is pinned from both directions — the Windows path survives, and
`javascript:` is still stripped, so the guard cannot be quietly widened into uselessness.

### An error state whose only exit reproduced the error

The export bar has nine states and a pure function that derives them. One of them says, in these
words: *"That file is no longer available — Export again."*

The button under that sentence re-issued the download that had just failed.

A 410 `export_file_gone` does not change the job row. That is deliberate and correct — the download
endpoint refuses and writes nothing. But the control decided what a click *meant* by asking the row:
`status === 'ready' && current` → download. The row still said `ready`, because nothing had told it
otherwise. So the button downloaded, got another 410, and displayed the same sentence again. A
reload did not help: the view derived to `ready` and offered *Download PDF*, which 410'd.

The copy promised a recovery the UI could not perform. The failure contract's own row says the next
action is to re-export; there was no way to re-export.

The root cause is not the 410 handling. It is that **the row was being interrogated twice** — once
for what to render, once for what a click does — and the two answers could disagree. The fix moves
the click's meaning onto the view, which is the one place that holds both what the server last said
*and* what this browser's own request just found out. The second derivation is deleted, so the two
can no longer disagree.

The test that existed asserted the sentence appeared. It asserted nothing about what the button did.
A docblock claiming a recovery its assertion cannot deliver is the same defect one level up.

### Five sentences nobody could read

Then the reviewer pulled the thread further and found the same defect one step earlier, worse.

`requestExport.isError` was read **nowhere** in the feature. The mutation was consulted for
`isPending` and `variables` and nothing else. So a refused `POST /exports` — five distinct rows of
the failure contract, each with a written *"User sees"* sentence — rendered nothing at all. The
control went from *Starting…* back to *PDF*, and the click looked as though it had not happened.

Three of those five commit **no row** by design: the per-session cap, the rate limit, and a database
failure before the enqueue. So there was nothing for the poller to surface either. The silence was
not first, it was permanent.

And the natural human response to a button that appears to do nothing is to press it again — which,
for the 429 and the session cap, is precisely the behaviour those limits exist to stop, and which
can never succeed.

A failure contract's *"User sees"* column is a promise. Fifty-seven rows were enumerated, and five
of them were promises to nobody. The enumeration was not the problem; nothing checked that the
enumerated sentences had a route to a screen.

### The test that could no longer fail

The last one I caused myself, which makes it the most instructive.

`render_html` was dead code — zero production callers — and it was the one function that built the
HTML document shell *without* the sanitize step, i.e. exactly the composition that day fourteen's
"sanitizer that ate its own document" proved wrong. Deleting it was right.

But three tests hung off it, and they had to be re-pointed somewhere. My instruction named the wrong
target; the agent noticed my wording was inconsistent, chose the sanitize-inclusive composition that
production actually uses, and I approved it. For two of the three tests that was an improvement. For
the third it was a disaster in slow motion.

That third test asserted *"the emitter produces only the eleven allowed tags"*. Run through
`sanitize_html`, that assertion is applied to the output of a function that emits only those eleven
tags **by construction**, against the same eleven-element set. It could no longer fail for any
emitter output whatsoever.

The proof took two minutes and is the only kind that counts: I broke the emitter's heading clamp so
it would write `<h4>`, and ran the file. **Thirteen passed.** With the clamp still broken, on a raw
token stream:

```
emitter output      : '<h4>Leaked</h4>'
after sanitize_html : 'Leaked'
```

The sanitizer removed the leaked tag along with its markup, so the subset assertion held trivially.

The module's own docstring had warned about this in advance, from the other direction: *"nh3 is the
second lock, not the first, and that is load-bearing… A test that only ever fed it the emitter's
output would pass for the wrong reason forever."* I had created the mirror image of the exact trap
the file warns about, three inches below the warning.

There is a second lesson underneath. Fixing it needed a fixture with `#### Heading Four` *and* a
parse that bypasses the pipeline's normalization — because `normalize_to_grammar` clamps `h4` to
`h3` upstream, so a Markdown fixture alone can never reach the emitter's own clamp. The emitter's
docstring promises the property holds *"for any stream anybody ever hands it"*. **Testing a promise
made about the back door requires going in through the back door.** A test that only enters the
front can never check it.

### Working with agents that get cut off, a third time

The session limit hit mid-round again, this time killing an agent before it wrote a single line. The
project already had a precedent for this from slice 1.3 — the coordinator finishes the work — so I
implemented both remaining changes myself.

That creates a different risk, and it is worth naming: the implementer and the coordinator became
the same party, which removes exactly the independence the review step exists to provide. The
mitigation was to say so explicitly when handing the work to the reviewer and ask it to judge the
code rather than the process note. It passed on the merits, and the vacuous-test defect I had caused
was caught by the reviewer, not by me — which is the argument for keeping that step honest even when
it is inconvenient.

The other discipline that paid off all round: **the implementer never edits a test.** Three times an
agent stopped and reported rather than touching one, and each time the test really was wrong. Once
it was six tests clicking a *disabled* button — `findByRole` resolves on the pending render, where
the control is still disabled, so the click was a no-op and no request ever fired. Those six had
produced a red that looked convincing (`Unable to find an element with the text…`) but proved only
an absence, exactly as an `ImportError` red does. What made them trustworthy afterwards was mutation
testing: corrupt one sentence and flip one `retryable`, and confirm exactly those two tests fail.

I did edit two test files myself, and said so in the commit bodies rather than hoping nobody looked.
The reviewer checked all three such edits across four rounds and confirmed no assertion moved. One
of them is worth keeping as a rule: a cast written to let a RED test compile against a type that
does not exist yet is legitimate scaffolding — and **a RED-phase escape hatch that outlives its RED
is indistinguishable from a suppressed error.** Remove it in the GREEN.

### The test that failed because the machine was fast

This one arrived after `/verify` had already passed, from CI, which is the right place for it to
arrive from.

The export slice has a test that asks a blunt question: while twenty CPU-bound renders run
concurrently, does the event loop still answer `/health/live` promptly? It hammers that endpoint on
a 1 ms cadence, collects the latencies, and asserts a **p50 under 5 ms**. Before it concludes
anything it checks it has at least twenty samples — because a p50 over three numbers is not a p50.

It went red on GitHub's runner:

```
only 16 /health/live samples were taken during the 20 concurrent renders
assert 16 >= 20
latencies: [0.00145, 0.00050, 0.00050, 0.00051, ...]
```

Read the numbers before the message. Half a millisecond. The loop was *superb*. The property the
test exists to guard held with enormous margin — and the test failed anyway, on its own
precondition, because the renders finished before the sampler could take twenty ticks.

Each worker rendered a fixed twelve times, a number chosen so the batch "lasts long enough for the
sampler to collect its floor". On a fast machine, 240 renders take under 16 ms. **So the guard got
harder to satisfy the better the machine performed.**

A day earlier the same test had failed on my laptop for the opposite reason — p50 drifting past its
bound under load — and I had written it up as "will eventually fail on a busy laptop". That was half
right in the way that is worse than being wrong: it named a real failure mode and implied it was the
only one. The flake was bidirectional. Slow machines trip the p50; fast machines trip the floor.

The floor itself was never the problem — refusing to draw a conclusion from too little data is
exactly right. The problem was **coupling it to how long the work happened to take**, which is
neither controlled by the test nor related to what it measures. The renders now run until the
sampler signals it has its twenty samples. The batch's duration became an *output* instead of an
assumption, and the count assertion became a self-check on something guaranteed rather than a race.

### The mutation that would not die

Then the verification turned up something better than the fix.

The rule in this project is that a test claiming to guard a regression must be observed failing when
you reintroduce it. So: drop the `asyncio.to_thread` hop from the renderer, making the render
synchronous on the event loop, and confirm p50 collapses.

It did not collapse. It **hung** — one core at 99.9%, zero responses, for eight to ten minutes,
until the pytest processes were killed from outside the container. The freshly-added
`asyncio.wait_for(..., timeout=30)` never fired.

The reason is worth carrying well beyond this test. A coroutine that wraps a synchronous call and
contains **no internal `await`** has no suspension point. `asyncio`'s cancellation is cooperative:
`CancelledError` is delivered *at* an `await` boundary. If there isn't one, there is nowhere to
deliver it, and nothing short of an OS signal interrupts the work. `wait_for` does not "stop" a
coroutine — it asks one to stop at its next opportunity, and a fully synchronous body never has one.

Two things follow. First, this is a *stronger* proof of the property than the one we went looking
for: not "the loop got slower" but "the loop stopped entirely, and the test harness's own timeout
could not save it". Second, and more usefully: **a `wait_for` hang-guard around CPU-bound work
protects only against a partial regression** — something slower, something that still yields
occasionally. Against the total case it is decorative. Making it real needs an OS-thread watchdog
with a hard `os._exit`, which was judged disproportionate here and written down rather than quietly
assumed away.

This is the async lesson CLAUDE.md has been warning about since slice 1.1, met in its purest form.
"A synchronous CPU-bound call inside an async route blocks the event loop for every concurrent user"
is the sentence. Watching a timeout fail to fire while a core pegs is the sentence with teeth.

### The common thread, an eighth time

Day fourteen's bugs lived between a contract and the thing on the other side of it. This round's
lived between **a test and the thing it claimed to be about**.

A corpus that scored 100 % on documents the bug could not affect. A test that asserted a sentence
appeared and nothing about what its button did. Fifty-seven enumerated failure rows, five of whose
promised sentences had no route to a screen. An assertion moved one function downstream and rendered
incapable of failing.

Every one of them was green. Every one of them was measuring something adjacent to the thing it was
named after. The tool that found each one was the same, and it is the only one that works: **take
the thing the test claims to guard, break it on purpose, and check that the test notices.** Five
times this round that check was run. Five times it changed the answer — and the fifth ran after
`/verify` had already passed, on a test that was failing while the thing it measured was perfect.

## What's next

Slice 1.5 is **verified and in review**: **1314 backend and 504 frontend tests**, green twice in a
row, the production image verified by building it and starting all three containers on it, and every
budget measured rather than assumed — inline downloads at a p95 of 11 ms against a 500 ms budget,
`POST` to a finished PDF at a p95 of 0.17 s against 10 s, and 100 % of the fixture corpus rendering to
all four formats. `/verify` took four rounds, found three MAJOR defects in a suite that was already
green, and the pull request is open.

Open, and named rather than quietly carried:
- **AC-37 forces an accessibility regression.** Pinning *"Preparing your PDF… 3s"* as one contiguous
  string means the count cannot hide in an `aria-hidden` span the way slice 1.3 hides it, so it is
  announced every second. Fixing it properly means relaxing the criterion.
- **AC-42's 401 copy ships without its link home.** Still blocked, and for a sharper reason than
  before: the view's union *was* widened during `/verify` — but the new tests re-pinned it with a
  whole-object `toEqual` in the same commit, so adding a link means editing a test again. A shape
  loosened and re-tightened in one motion is no looser than it started.
- **X-46's stale-download log line is unbuilt.** The contract promises a line when the API knowingly
  serves an older file; the router logs nothing. Building it means widening a use case's return type
  to carry a cross-aggregate fact, for one log line. The reviewer's recommendation is to strike the
  cell instead — `current` is already reported on every one-second poll, so the line would be a
  *derived copy* that can disagree with the resource. If the operational question is ever genuinely
  asked, a domain event is the vehicle.
- **A test lies to a settings field.** `model_copy` writes a float into an `int`, and does not
  validate, so the render-timeout branch is proved against a value the type forbids. A field a test
  must lie about is a field that wants to be a float.
- **`test_posting_fetcher_event_loop.py` carries the same latent shape** AC-11's liveness test just
  had to have fixed: a "at least 20 samples" floor coupled to how long a fixed batch of work happens
  to take. It has not flaked, probably because network I/O plus `trafilatura` is slow enough that the
  sampler always gets there first. Fix it the same way if it ever flares.
- **`markdown_it` is an unsilenced vendor logger sitting on the raw CV.** Measured: it does not leak
  today — its debug records are counts, not text. That is the same "clean today" argument the
  `httpcore` note already makes, on the one library holding the document.
- **Max-latency under concurrent render load is unverified** (see the measurement that overruled the
  criterion, above).
- **The production image has no `pytest`**, so running the slow render check inside it needs a
  per-invocation install. Baking test tooling into the shipped image is the wrong direction; a `test`
  build target on top of `production` is the call, and nobody has made it.

Still carried from earlier slices:

- **Startup refusals that never exit under uvicorn** (now three of them — 1.5 added the export
  stale-window guard): `devops`, before the deploy SSH secrets are set.
- **Very long CVs** can exceed the per-attempt timeout: a later slice, measured first.
- **Beat isn't monitored**, because `/health/ready` can't see it: slice 1.6, alongside the purge.
- **The editor is not lazy-loaded.** 530 kB of TipTap and friends load on the workspace where nobody
  edits. Whoever next measures first paint decides.
- **A paused document whose author leaves** sends into the 429 window and may lose the text if refused
  again; a `leaving`-with-timer state is the model if it is ever wanted. Noted in the machine.
- **The deploy path is still unproven, but the gate is real** — corrected on 2026-09-19 by reading
  the GitHub API instead of these notes. The `production` environment carries a required-reviewer
  rule and a protected-branch policy, and the repo holds no secrets at all, so a merge to `main`
  builds and then waits for a human. The earlier note here said the opposite, and it was repeated
  through an entire slice after it had stopped being true. The habit that caused it is the one worth
  naming: a control you have read about is not a control you have checked.

Next is 1.6: retention. It is the slice that finally deletes things, and 1.5 handed it two
guarantees to build on — every export row cascades from its guest session, and every file's key is a
pure function of the row's id and format, so the purge can find a file from a row, or reconstruct one
from an id after the row is gone. It also handed it a new problem: the uploads volume now holds output
as well as input, written by a different container than the one that reads it.

The three obligations 1.4 wrote down rather than met are met. The first HTML string in this product's
document path exists now, lives for the length of one function call, and never reaches a log, a row, a
response or Sentry.

The specs die when the features ship. This file doesn't.

---

# Slice 1.6 — retention, or: the first thing that deletes

The previous chapter ended by saying 1.6 is "the slice that finally deletes things." That turned out
to be the least interesting thing about it.

Here is the honest summary. The purge itself — find expired sessions, delete the rows, unlink the
files — is about forty lines of application code and it was right almost immediately. What took the
slice was everything around it: the tests that looked like they were checking something and weren't,
the fixture that hung CI for twenty minutes, a file-deletion bug that had been latent since 1.1, and
a two-second health check that nobody had ever timed.

## Deleting is easy. Knowing you deleted the right thing is not.

Start with the shape of the problem, because it explains every decision that follows.

A purge touches **two systems that cannot be committed together**: rows in Postgres, and files on a
volume. There is no transaction spanning them. So you have to choose which one goes first, and the
choice is really a choice about *what survives a crash in between*.

- Unlink the file first, crash, and you have a **row pointing at nothing**. A user clicks Download
  and gets an error. That row is a lie, and nothing can find it except the user who trips over it.
- Delete the row first, crash, and you have a **file nobody references**. Invisible, harmless,
  occupying disk — and findable later, because the filename is a UUID and the layout is walkable
  without the database.

ADR-0006 chose rows-first back in Phase 0, and this slice is where that sentence had to become code.
"Rows first, *committed*, then files" — the word *committed* is doing real work. An uncommitted
delete followed by an unlink gives you the broken-download case anyway, on a rollback instead of a
crash. So the commit is per-session, in the composition root, and the use case never names a
transaction at all (it may not — that's ADR-0002's layering rule).

The analogy I keep coming back to: it's like demolishing a building and then cancelling the parking
permit, versus cancelling the permit and then demolishing. Both orders end in the same place. Only
one of them leaves a hole in the ground that nobody has the paperwork for.

## The bug that three passing tests were happy with

Now the part worth the price of admission.

A file in this system is addressed by a `FileRef` — an opaque key with a strict grammar, ending
`.pdf`, `.docx` or `.txt`. When we write a file we write it as `<key>.part` first, `fsync`, then
`os.replace` it onto the real key. That rename is atomic, so a reader never sees half a file, and a
crash mid-write leaves a `.part` for the sweep to collect. Standard, careful, correct.

Here is the thing nobody noticed: **a `.part` file cannot be a `FileRef`.** The grammar forbids it.
So when the orphan scanner finds `abc.pdf.part`, it hands back the *base* ref (`abc.pdf`) plus a
separate `is_partial: true` flag. Perfectly reasonable.

And then the sweep called `files.delete(ref)` — the same call it uses for every other file.

`LocalFileStore.delete` unlinks `root/<ref.key>`. The **final** key. Not the `.part`. With
`missing_ok=True`, so it doesn't even complain.

Two consequences, and the second one is the bad one:

1. The `.part` is never deleted, and is reported as `reclaimed`. It comes back every sweep, for
   ever. The report says a thing that did not happen.
2. **If a file exists at that final key, the sweep deletes the live file.** And partials are
   deliberately excluded from the "is this referenced?" cross-check — because nothing ever
   references a `.part` — so the one guard designed to prevent exactly this *cannot fire*.

Is that reachable? Yes, and not exotically. `put` #1 succeeds, so `K` exists and a `ready` row points
at it. The Celery task is redelivered (which happens — `task_acks_late` doesn't mean what people
think). `put` #2 starts, writes `K.part`, and the worker dies before `os.replace`. Now both exist,
`K` is live and referenced, and the next orphan sweep deletes it.

**The test for this behaviour passed the whole time.** It asserted against a recording fake — a stub
that logs "you called `delete` with this ref". A fake has no filesystem. It cannot tell `K` from
`K.part`, because on a fake there *is* no difference. The test looked like it guarded the behaviour
and structurally could not.

The fix took ten minutes. Finding it took rewriting the test to drive a **real** `LocalFileStore`
over a real temp directory, with real `os.utime` ages — at which point it failed instantly with
`assert not part_path.exists()` → `assert not True`, and then, worse and better,
`AssertionError: the live, referenced file must survive the .part reclaim`.

The lesson is already written in this repo, in slice 1.5's notes and in the R-42 row of this slice's
own failure contract: *assert against direct filesystem checks, not against the report the code
produced*. It was written down. It still happened. Writing a rule down is not the same as having it
reach the moment where it applies.

**And the fix itself had a fork in it.** The obvious move is `delete(ref, partial=True)` — one
method, one flag. I went with a second method, `delete_partial(ref)`, and the reasoning is worth
keeping: a boolean would have fixed this *instance* and preserved the *shape that caused it*. One
call site, one flag threaded in from a variable, and the wrong value silently deletes a stranger's
CV. Two differently-named methods cannot be confused by a caller who forgot which way the flag
pointed. When the failure mode is "silently destroys data", spend the extra method.

A nice side-effect: adding one method to the `FileStorePort` Protocol produced **twelve
`mypy --strict` errors across six files** — every test double that claimed to implement the port. The
contract change proved its own reach. That's the argument for the port being a `Protocol` in
`domain/` rather than an informal convention, made concretely rather than theoretically.

## The test that hung CI for twenty minutes

Second story, shorter, and it's a pure Postgres lesson.

A test needed to force a `DELETE` to fail mid-batch, to prove the SAVEPOINT contains the failure and
the rest of the batch survives. The way to do that is hold a row lock from another connection and set
`lock_timeout` so the purge's delete gives up quickly.

It hung. Not failed — hung, producing zero output, until I killed it by hand.

`pg_stat_activity` told the story: one connection `idle in transaction` holding the lock, two others
blocked on `Lock: transactionid` with an unbounded wait. The `SET lock_timeout` had simply not
applied.

Why: **`SET` is per-connection, and the purge commits per session.** SQLAlchemy's `QueuePool` checks
a connection back into the pool at the end of each transaction and may hand you a different physical
connection next time. By the time the loop reached the locked row, it was on a connection that had
never seen the `SET`.

Two-part fix, both parts necessary:
- **Pin one physical connection** for the whole test (`engine.connect()` once) and bind the
  sessionmaker to *that*, not to the engine.
- **Commit the `SET`** — because a bare `SET` is itself transactional and reverts on rollback.

Every pinned connection in that file now also carries `statement_timeout = 5s`, so any future
accidental lock fails in seconds with a Postgres error naming the timeout, rather than hanging.
Because here is the thing about a hanging test: **it is worse than a failing one.** A failure names
its cause in the output. A hang produces nothing, CI sits on it until the job timeout, and whoever
picks it up starts from zero.

## Two ways to fail, pointing in opposite directions

This slice has two mechanisms that can't reach Redis or Postgres, and they fail **opposite ways** on
purpose. A reader who meets only one of them will "fix" the other for consistency, so both carry the
argument at the point of the code.

- **The purge lock fails OPEN.** Redis down? Run the purge anyway. The cost of a skipped purge is a
  broken privacy promise that compounds every hour. The cost of two overlapping purges is duplicated
  work on a job that is idempotent by construction — a second `DELETE` affects zero rows, and
  `delete` is `missing_ok`. The lock is *advisory*; it is not what makes concurrency safe.
- **The orphan sweep's cross-check fails CLOSED.** Can't ask the database whether a file is
  referenced? Delete nothing, exit 1. Because deleting a file because you *couldn't ask* is the one
  irreversible mistake this tool can make.

One sentence generates both: **put a mechanism's failure on the side whose loss is recoverable.**
Duplicated work is recoverable. A deleted file is not.

There's a subtlety I'd have missed without writing it out: the lock had to return **three** states,
not a boolean. "I got it", "someone else has it", "I couldn't ask". A boolean collapses the last two,
and then Redis hiccuping looks identical to a purge already running — so the job skips, logs
"skipped", and exits 0. Which is precisely the trap: a check asserting "the purge ran successfully"
passes against a run that never happened.

## A number nobody had ever measured

The acceptance criteria said to measure `/health/ready` against its 300 ms budget. So I did.

**2143 ms p95.** Seven times over.

Before blaming the new probe, I timed each one separately: `postgres` 0.7 ms, `redis` 1.1 ms, the new
`guest_purge` probe **1.8 ms**… and `celery` **2128 ms**.

`control.ping(timeout=2.0)` is a broadcast with **no reply limit**, so it waits the full two seconds
regardless of how fast the worker answers. Proved it three ways on the same box, one worker running:
`ping(timeout=2.0)` → 2038 ms. `ping(timeout=0.5)` → 511 ms. `ping(timeout=2.0, limit=1)` → **4.0 ms**,
returning *the same single reply*.

This endpoint has cost two seconds since Phase 0. Nobody noticed because nobody timed it — it always
*worked*, and "works" and "works within budget" are different claims. The deploy's readiness gate
polls it.

I did **not** fix it, and that restraint is the interesting part. `limit=1` is one keyword, but it
changes an existing probe's observable output: `detail` currently reports `"N worker(s)"` and with a
limit it would always say one. The probe's real contract is "did *any* worker answer" — so the count
is a nicety — but trading it away is a decision for whoever owns that endpoint, not something a
retention slice should smuggle in. It's recorded with an owner and a trigger instead.

That's the discipline I'd want applied to my own work: **measure everything the criteria name, report
what you find even when it isn't yours, and don't quietly widen your own scope to fix it.**

## Small things worth keeping

- **`NotImplementedError` subclasses `RuntimeError`.** A red-first test asserting "an unexpected
  exception propagates" with `pytest.raises(RuntimeError)` passes *vacuously* against a skeleton
  that raises `NotImplementedError`. It looks like a passing contract test and proves nothing. Pin
  the exact type.
- **A skeleton that does nothing satisfies every "absence" assertion.** "The dry run never calls
  delete" is trivially true of a method whose body is `raise NotImplementedError`. Every absence
  assertion needs a positive one beside it that actually discriminates.
- **A method that ignores its own field looks like a bug.** `RetentionWindow.expiry_cutoff(now)`
  returns `now` and never reads `hours`, because the window was already frozen into `expires_at` when
  the session started. The plausible "fix" — subtracting the window again — turns a 24-hour promise
  into a 48-hour one *with no symptom whatsoever*: the job still runs, still logs, still deletes,
  just a day late. Three parametrized tests over 1h/24h/999h exist solely to catch that edit.
- **The dev uploads volume had 753 orphaned files out of 827.** The arithmetic reconciles exactly in
  both directions, which is how you tell residue from a broken cross-check.
- **The test database had a committed, already-expired session in it** — so `count_expired` returned
  1 on a supposedly empty database. Scope assertions to ids your test created; an absolute count is
  one stray row away from lying to you.

## What `/verify` found, or: four ways to promise something and not check it

The slice went into `/verify` with 1423 passing tests and came out with 1456. That arithmetic is the
least interesting part. What matters is that all four things review found were *the same kind of
thing*, and it is a kind that a green suite is structurally incapable of noticing.

Every one of them was **something the spec promised that no test asserted.**

Not a bug. Not a design flaw. A gap between a document that says "this is guaranteed" and a codebase
where nothing checks it. The suite was green because the suite never asked.

- **The beat entry and its Celery task had no test whatsoever.** Five acceptance criteria — the
  schedule, the interval, the queue, the thinness of the task, the absence of retries — and not one
  line of test between them. Including, and this is the part that should make you wince,
  `GUEST_PURGE_ENABLED`: the single control keeping the first `DELETE` in this codebase off an
  automatic schedule before anyone has rehearsed it. **Nothing proved the flag worked.** It happened
  to. Nobody had checked.
- **AC-38's privacy test did not exist**, and had never even been given a task number. Meanwhile the
  spec's Privacy section said the "never logged" list was *"asserted by AC-38's planted-marker test,
  **not by intention**."* That sentence had been false since the day it was written. It was written
  precisely to stop someone relying on intention, and then it became the thing being relied on.
- **R-3 and R-4 named two log events that existed nowhere in the source.** More on this below,
  because the consequence is funnier and worse than it sounds.
- **The symlink seam** — a whole story of its own, three sections down.

If you want one transferable idea from this slice, it is this: **a specification is not a test, and a
sentence in a spec claiming a test exists is worth less than no sentence at all** — because the empty
spec leaves you suspicious, and the confident one buys your trust for free.

## The job whose failure mode is silence, failing silently

R-3 and R-4 were rows in the failure contract. Each specified a log line: when one session's `DELETE`
is refused, log `retention.session_purge_failed` with the session id and the error type. Neither
event name appeared anywhere in `api/src/`. And the handler was worse than missing — it was this:

```python
except Exception:          # note: no `as exc`
    sessions_failed += 1
    continue
```

The exception was never bound. So `error_type` wasn't merely unlogged, it was **unrecoverable in
principle**: by the time anyone wanted it, the object was gone.

Now follow it through, because this is where it stops being a missing feature and becomes the slice
contradicting its own reason to exist. The CLI's batch loop ends with:

```python
if limit is not None or report.sessions_deleted == 0:
    break
```

Perfectly sensible — a batch that deleted nothing has nothing left to try. But candidates arrive
oldest-expiry-first, and a session whose `DELETE` refuses *permanently* (a lock that never clears, a
constraint nobody predicted) is never deleted and therefore never leaves the front of the queue. So
every hour, for ever: the job runs, tries it, fails, deletes the others, stops. The run **exits 0**.
`overdue` sits above zero and never falls. And nothing, anywhere, names the session or the reason.

This slice exists because *"a job whose only failure symptom is silence cannot be silently broken."*
That is the sentence in the spec. And the purge had built itself a silent failure one level down,
inside the very mechanism meant to prevent one.

The fix is the interesting part, because the obvious repair was wrong. There is a `structlog` call
three lines below that handler. Using it would have worked and would have broken the architecture:
`application/` does not log, and — less obviously — **a log line emitted from there is a record the
AC-38 privacy test cannot see.** Instead the use case *returns* its failures (`PurgeReport` grew two
tuples of frozen value objects) and the two entry points emit them. The layer stays silent, and every
record the system writes stays inside the privacy test's field of view.

That meant widening `PurgeReport`, whose acceptance criterion pinned it to "counts and instants only"
with a test asserting the exact field set. **That test went red, which is the mechanism working.** The
widening got argued in a spec amendment before the test was touched — what forced it, why the
criterion's *intent* survives (a session id and a class name are not text a user wrote), and what
rejected alternative was considered and why its precedent didn't transfer. Then the test was widened.

The order matters: argue, then widen. A test that changes in the same commit that makes it pass is
the failure the whole red-first cycle exists to prevent.

## The same bug three times, or: the thing named is not the thing acted on

You already met the `.part` bug earlier in this chapter — the partial file that survived while a live
file beside it was deleted. During `/verify` the same shape turned up **twice more**, both in the file
store, and it is worth laying all three side by side because the pattern is the lesson.

The review flagged something small: `_resolve_contained` calls `Path.resolve()`, and `resolve()`
follows symlinks. Which means the path handed to `unlink` is the link's **target**.

Here is why that was newly dangerous, and it is a genuinely subtle bit of reasoning. `resolve()` had
been there since slice 1.1 and had never been a problem, because `delete` was only ever called with a
`FileRef` derived from a **database row**. The orphan sweep changed the premise: it is the first
caller in this codebase that deletes by a name it **discovered on disk**. A pre-existing line became a
deletion primitive the day a new caller arrived with untrusted input.

Plant a symlink at a `FileRef`-shaped key pointing at another session's live CV, and: the scanner
reports it (the name parses), the cross-check clears it as an orphan (*the link's* key is in no row),
and the sweep "reclaims" it — **destroying a live, referenced file belonging to a different user**
while the link itself sits there untouched and the report cheerfully says `reclaimed: 1`.

The scanner, incidentally, was blameless. It passes `follow_symlinks=False` to everything and never
descends a link. Two components either side of one seam, each locally correct, with **opposite
symlink policies** — and the bug living in the gap. Its comment even claimed a link "cannot walk this
job out of the store root," which was true of the walk and quietly false of the pair.

Then, following the fix rather than closing the ticket, a third instance surfaced. `_put_sync` opened
`<key>.part` with a plain `open()`. The containment guard covered the *final* key; `.part` is a
different name, so it never applied. So: bytes get written through the link onto someone else's file,
and then `os.replace` — which renames a link rather than following it — **installs the symlink at the
real key**, permanently. One upload, an arbitrary file overwritten, and that storage key is a symlink
from then on.

Three instances, one sentence: **the thing named is not always the thing acted on.** And all three
were invisible to a recording fake, because a fake has no filesystem and therefore no difference
between `K` and `K.part`, and no difference between a link and a file. Every one was found by a test
driving a **real** filesystem.

The repair is uniform, which is the part I'd want to remember. Rather than four arguments about four
methods, the module docstring now makes one claim: `put` and `get` open `O_NOFOLLOW` and work on the
**descriptor**; `delete`/`delete_partial` call `unlink`, which removes a link and never its target;
and `_resolve_contained` refuses a final-component link — *but it is a check-then-use, so it is the
outer lock and never the only one.* That last clause is the whole reason `O_NOFOLLOW` is there: the
check and the use are two syscalls, and a name can change meaning in between.

Two details worth stealing:

- **`os.fchmod(fd, …)` instead of `os.chmod(path, …)`.** A descriptor cannot be re-pointed between
  the open and the chmod. It is also immune to the umask, which `O_CREAT`'s mode argument is not — so
  the file is now *created* at `0600` rather than created world-readable and narrowed a moment later.
- **The descriptor-leak fix removed code.** Both paths now pass one `_nofollow_opener` to `open()`'s
  `opener=` hook. `open()` takes ownership of the descriptor the opener returns, so CPython closes it
  if anything downstream raises. The leak is gone *by construction* instead of by a handler, at both
  sites, and `put` lost a local variable in the process.

## The test that was pinning the wrong lock

One more, because it is the most instructive failure of the round and it was mine.

A test called `test_get_translates_eloop_to_file_store_unavailable_not_stored_file_missing` planted a
symlink, called `get`, and asserted the right exception type. It passed. Its docstring explained that
the symlink makes `open()` raise `ELOOP`.

It does not. `_get_sync` evaluates `_resolve_contained(ref)` as the *argument* to `open()`, and that
check refuses the link first — so the exception came from the containment check, `__cause__` was
`None`, and no `OSError` was ever involved. The assertions were all true. They were pinning the outer
lock for the third time, while the docstring told any future reader that the opener was covered.

Delete `opener=_nofollow_opener` and that test stays green, under a comment vouching for it.

This is CLAUDE.md's own rule — *a docblock claiming coverage the assertion cannot deliver is the same
defect one level up* — in its nastiest form, because the false comment is worse than the weak test.
The fix was to make the test bypass containment so it hits the path it names. The proof is that it
now **reddens when the opener is removed**, and it demonstrably did not before.

If you take one habit from this: when you write a test for a new guard, delete the guard and watch
the test fail. If it stays green, you tested something else.

## Two mistakes of mine, recorded because they cost time

**I reported a test failure that wasn't one.** Running the suite twice in a row is an acceptance
criterion here — a second run that fails means Redis state leaked. I ran it, saw four failures, and
reported AC-40 as failing. It wasn't. I was running the suite while a review agent ran its own gates
against the same containers, and `clear_redis` calls `flushdb()`, which is **global**. Each run was
deleting the other's rate-limiter counters mid-test; the limiters fail open, so a third request that
should have been `429` came back `201`.

The evidence was in front of me before I reported it: **a different set of unrelated tests failed each
run, and every one passed in isolation.** That pattern means shared-state contention, essentially
always. It does not mean a defect in the code under test. I read it the wrong way round.

The durable lesson isn't about me, though — it's that **this suite cannot be run concurrently and
nothing tells you so.** Reach for `pytest -n`, or just a second terminal, and you get what looks
exactly like flaky application code. That is now written in `clear_redis`'s own docstring (with the
fix, if it's ever needed: a per-run key prefix, not a narrower flush, which would bring back the
leftover-lock hazard) and in CLAUDE.md beside the other Redis warnings.

**I fixed a false claim in two places and missed the third.** A docstring asserted that `error_type`
could only ever be a class name *structurally*, because `from_exception` was "the only way this value
is built." Not true: the dataclass constructor is public, and the tests use it directly. It is a
convention the call sites keep, not a property the type enforces. I corrected it in the value object
and in the spec — and review found the same claim surviving verbatim in `log_events.py`, in its
strongest wording, plus a softer fourth copy in the use case.

Four copies of one sentence, and I'd fixed half of them. When a claim is worth stating in more than
one place, `grep` for the claim before you declare it corrected — the copy you miss is the one someone
relies on.

## An inconsistency I chose to ship, and wrote down

The symlink fix routes a planted link into the sweep's `unrecognized` count, where it is skipped and
never deleted. Correct behaviour. But `unrecognized` now means two things: "a stray `.DS_Store`
someone dropped in" (residue, ignore it) and "a symlink at a key the store would never create, so
something *else* wrote to this volume" (investigate this now).

A count that means two things is a count nobody acts on, and the weaker meaning is the one that gets
assumed.

What makes this properly awkward is that **this is the exact argument the slice had already used to
refuse folding R-38 into `failed`** — *"folding 'we could not even look here' into it would make one
number mean two things."* Same reasoning, applied in one place and not the other.

The honest fix is a second counter. The call was to document the limitation instead — the two causes
are indistinguishable from the report, here is the `find -type l` that tells them apart, and the
counter is the fix if a planted link is ever actually found. That trade is defensible (the trigger
requires prior write access to the volume) and it is still an inconsistency.

It is in the spec, in writing, labelled as the cost of shipping now. Which is the point: **a known
inconsistency that is written down is a decision; the same inconsistency undocumented is just a
mistake nobody has met yet.**

## The test I wrote to catch a bug, which could not catch that bug

This one is my favourite thing that happened in the whole slice, and it happened after `/verify` had
already said PASS.

There was one automatable task left: AC-42, the criterion that says the orphan sweep's directory walk
must not block the event loop. The claim it guards is a real one. The sweep walks a whole volume —
thousands of `stat` calls — and if that runs on the event loop instead of in a worker thread, every
other user of the app waits for the whole walk. No error, no log line, nothing on a dashboard. The
app is just *slow*, for everyone, until the sweep finishes.

So I wrote the test the way the spec asks and the way the two existing tests in the codebase do it:
start hammering a trivial health-check endpoint, start the heavy work alongside it, and assert the
health check's median response time stays under five milliseconds. Blocked loop, slow health check.
Obvious.

It passed. Green, first try, 0.33 ms median against a 5 ms budget.

That should have been a good moment, and this project has trained me to distrust good moments. There
is a rule in `CLAUDE.md` that exists precisely for this: *if a test says it guards a regression,
verify it fails when you reintroduce that regression.* So I broke the scanner on purpose — took the
`asyncio.to_thread` out of the walk, which is the entire mechanism the criterion is about — and ran
the test again.

**It passed again.** Median under a millisecond. A test written to catch exactly one defect, run
against exactly that defect, reporting that everything was fine.

### Why it could not fail

The tell was in the corner of the screen. The healthy run took 3 seconds. The broken run took 27. The
blocking was massive, and it was visible the whole time — just not to the assertion.

Here is the mechanism, and it is worth internalising because it is not specific to this codebase.

The test drives the app *in-process*. There is no real HTTP, no socket; the test client calls the
application's coroutines directly. And `/health/live` deliberately does nothing — no database, no
Redis, no disk. Put those two facts together and you get something unexpected: **a request to that
endpoint never hits a real suspension point.** It runs from start to finish through nested `await`s
that never actually give the event loop a chance to go and do something else.

Which means the event loop can never switch to the blocking work *in the middle of a request*. A
request that starts while the loop is free finishes while the loop is free, at full speed, no matter
how long the loop was frozen before it started or how long it will be frozen after.

I was timing the one window in which the loop is, by construction, not blocked.

It is a bit like testing whether a doctor's waiting room is backed up by timing how long the
appointment takes once you are in the room. The appointment is always seven minutes. The three hours
you spent in the waiting room never appear in the measurement — and the waiting room is the entire
thing you wanted to know about.

### The fix, which is one line and a rename

Measure the wait, not the appointment. Each sample now times a one-millisecond nap **plus** the
request, and subtracts the nap. What is left is the loop's *turnaround*: how late the timer fired,
plus how long the answer took. A healthy loop is late by microseconds. A loop stuck inside a 300 ms
synchronous directory walk cannot answer until it is done, and now the number says so.

Same mutation, same scanner, same everything else:

| | median turnaround |
|---|---|
| walk in a thread (correct) | **0.62 ms** |
| walk on the loop (the bug) | **1263.71 ms** |

Two thousand times apart. That is what an assertion that can actually fail looks like.

### The part that made my stomach drop

If the brand-new test had this defect, what about the two that had been in the repo for months?

I checked the one for the job-posting fetcher — AC-10, written back in slice 1.2, with a beautiful
two-paragraph docstring explaining exactly which property it pins. Same in-process client, same
no-I/O endpoint, same median-of-request-times.

I broke *its* mechanism — took the `to_thread` out of the HTML extraction — and ran it.

Green.

That test had been passing for four slices. It had never once been able to fail.

### A second thing, which decided a number

Fixing it was not just a matter of swapping in the turnaround measurement. That helped and was not
enough, and the reason is a property of this kind of measurement that I had not thought about before:

**Blocking suppresses sampling.** While the loop is frozen, the sampler cannot take samples. So the
frozen periods are systematically *under-represented* in the very dataset you are computing a median
over. A median is only sensitive to blocking when the loop is blocked more than half the time.

For the orphan scanner that is satisfied — the walk starts instantly and blocks continuously, so
every sample lands on it. For the fetcher it is not: a fetch is *download first, then parse*, and
only the parse is the part under test. The first twenty samples all land in the download phase,
before the thing being tested has even started, and the median over them is a median over a warm-up.

The fix was to make the fetches loop until the sampler has its samples, and to raise the sample count
from 20 to 200 so the median lands inside the parse phase. Measured, not guessed — at 20 the mutation
passes, at 200 it fails, four runs out of four.

And I wrote the margin into the file instead of declaring victory, because the four mutated medians
were 6.6, 16, 18.7 and 273.7 ms. All red against the 5 ms budget — but that 6.6 clears it by only
1.3x, so a fast enough machine could still let that regression slip through. The honest summary is
*"this now catches the bug, and here is how much room it has"*, not *"fixed"*. The statistic that
would separate them cleanly is the loop's **unavailable fraction** over the window rather than a
median, and switching to it changes what those criteria assert — so it is written down with an owner
and a trigger, and left for whoever next touches those adapters.

### What to take from it

Three things, in order of how much they will cost you elsewhere.

**A green test is evidence about the test, not only about the code.** This project already knew that
in the abstract — there is a whole section in the story above about a test that could no longer fail.
This is the same lesson arriving through a different door: not a test whose assertion had been
re-pointed at something trivially true, but one whose *instrument* was pointed at the wrong window
from the day it was written.

**Wall-clock was screaming and nobody was listening.** 3 seconds versus 27. The information was on
screen for every one of those runs. When a run's duration changes by 9x and its assertions do not
move at all, the assertions are measuring something other than what changed.

**Performance assertions need mutation-testing more than logic assertions do, not less.** A wrong
logic test usually fails loudly on something. A wrong performance test passes serenely forever,
because "fast enough" is the default state of almost any measurement you can accidentally take. The
only way to know a latency assertion works is to make the system slow on purpose and watch it go red.

## The rehearsal, or: the number that could not be faked

The purge ran for real today, on the dev box, against real data. Here is what that was like, because
the *shape* of it is the transferable part.

**Two backups, not one.** A `pg_dump` and a tarball of the uploads volume, because restoring either
one alone is not a rollback — rows without files is a broken app with a green restore, files without
rows resurrects exactly what you deliberately deleted. The tarball held 827 files, which matched the
volume's own count, so I knew the snapshot was complete rather than partial. That check took three
seconds and would have been the difference between a backup and a belief.

**Then the dry run, and the first surprise.** 11 overdue sessions, 74 file keys. The runbook's next
step says `make purge limit=50` and *"confirm the backlog fell by exactly 50."*

With 11 sessions, `limit=50` takes all of them. The step would have "passed" — it would have run, it
would have deleted things, it would have exited 0 — and the safeguard it exists to be, a small
reversible-in-scope bite you verify before committing to the rest, would simply not have happened.
The command was written for a backlog nobody had measured yet.

So the step is now `limit=N where N is smaller than the backlog you just read`, and the reason is in
the runbook next to it. **A runbook step with a hardcoded number is a guess about the future** — the
number should come from the step before it.

**The bite: `limit=5`.** Before running it I picked the oldest session and wrote down everything I'd
need to check afterwards — its id, its base CV's id, and the storage key derived from that id by
hand. That last one matters more than it looks: the key isn't stored anywhere, it's *computed* from
the row. After the purge the row is gone, and with it your ability to know which file to go looking
for. **Capture the evidence before you destroy the thing that generates it.**

Then four independent confirmations, which is three more than the step asks for:

- the report: 5 deleted, 45 keys removed, 0 failures
- the backlog: 11 → 6, exactly −5, read from `/health/ready`
- the spot-checked session: zero rows in all four tables *and* the session row, and its file gone
- the volume's own file count: 827 → 782, which is exactly 45

The fourth one is my favourite because it costs one `find` and it is the only check that doesn't
trust the application at all. The report is the job's opinion of itself. The filesystem isn't.

**The full run, then the sweep.** 6 more sessions, 29 more keys. 45 + 29 = 74 — the number the dry
run predicted, closing exactly. Backlog 0. Then the orphan sweep took the 753 files that had been
accumulating since slice 1.1, and the volume went to zero.

And the arithmetic closed from both ends at every step: 827 on the volume, 74 referenced, 753
orphans; purge removes the 74, leaving 753; sweep reclaims 753, leaving 0. The `referenced` count
went 74 → 0 *because* the purge had just deleted the rows those keys belonged to — the two jobs'
halves confirming each other rather than two numbers that happen to match.

### The thing this slice was actually about

The dev database is now empty of guest data. For a moment that looked alarming, so I checked it
rather than reassuring myself: the pre-purge dump holds exactly 11 guest sessions, and all 11 were
expired. There was nothing unexpired for the purge to have spared. Empty is the correct answer.

**Verify the scary-looking outcome against the backup you took ten minutes ago, instead of
reasoning about whether it should be scary.** The whole reason to take the backup first is so that
this question has an answer.

### Flipping it on, and the command that would have lied to me

`GUEST_PURGE_ENABLED=true` went into `.env`, and then I nearly reached for `docker compose restart
beat`.

That would not have worked, and — this is the bad part — it would not have *looked* like it hadn't
worked. `env_file:` is read when a container is **created**, not when it starts. A restart keeps the
old environment. You'd flip the flag, restart, see beat come up clean, and have a schedule that is
still off. `up -d` recreates, and recreating is the point.

And `api` belongs in that list next to `beat`, which is less obvious: the API is what serves
`scheduled` on `/health/ready`, and the status panel repeats it to users. Recreate only `beat` and
the job runs while the UI tells everyone it doesn't — the exact dishonesty the panel was added to
prevent, achieved by being too careful about which container to touch.

Then a tick, end to end: I published the same message beat publishes — same task name, same options
read out of the beat entry itself rather than retyped — and the worker took it in 47 ms.
`examined=0`. Nothing to delete, because the backlog was 0.

And `/health/ready` moved anyway: `last_run` set, `last_outcome: "ok"`, **`stale: false`**.

**A run that deletes nothing is otherwise indistinguishable from a run that never happened.** That
sentence is the whole slice, and this is the first time the system said it out loud.

### The last bug, found by the fix

Except `stale` had been `true` a minute before that, with `last_run: null`, on a system that had
successfully purged fifteen minutes earlier. Nothing had failed. So where did the heartbeat go?

**I committed.** The pre-commit hook runs `make check`, `make check` runs the test suite, and the
suite's `clear_redis` fixture calls `flushdb()`.

Against the dev Redis. The `settings` fixture overrides `database_url` and `upload_dir` — and not
`redis_url`. There is no test Redis. The fixture's own docstring opens with *"Flush the test
Redis"*, and that thing does not exist.

This is a trap this project had already met one store over: `get_settings()` under `APP_ENV=test`
still hands back the *dev* `database_url`, and a cleanup script once emptied the dev database through
exactly that hole. Same shape, different datastore, and the existing warning in `CLAUDE.md` is about
`flushdb()` being global between *two concurrent test runs* — true, and one scope too narrow. It is
also global against the running application.

The consequence lands precisely where it hurts most. The purge's alarm is `stale`, and `stale` is
computed as "scheduled, and no heartbeat recently". Wipe the heartbeat and you get `stale: true` on a
perfectly healthy system — **AC-33's rule working exactly as designed, on a false premise.** Now that
the schedule is on, every commit fires the alarm.

It's dev-only; nobody runs pytest against production. But the lesson generalises past this repo:

**Test isolation is a property you have to check store by store.** Postgres was isolated, the
filesystem was isolated, and Redis looked isolated because the fixture said so in a docstring nobody
had reason to doubt. The isolation was three-quarters done, and the missing quarter was invisible
until a job that writes to Redis had to survive a commit.

The fix is a test-only `redis_url` beside the `test_database_url` that already exists. Not a narrower
flush — that would bring back the leftover-lock hazard `clear_redis` was written to kill, where a
stale lock makes the job skip, log "skipped", and exit 0, so a test asserting a successful run passes
against a run that never happened.

Which, you'll notice, is the same failure as the heartbeat one: **something that did not run,
looking exactly like something that did.** Third time in this slice. That's not a coincidence, it's
the subject.

## What is not done, and why that matters

The purge is built, tested, rehearsed and **on**. That last word was only earned today, and the order
it was earned in is the point.

`GUEST_PURGE_ENABLED` shipped `false` — because a purge is an irreversible `DELETE` across two
systems with **no rollback**, and the first automated run should not also be the first run. The
rehearsal was a human sequence: dump the database *and* snapshot the volume, dry run, a bite smaller
than the backlog, confirm it fell by exactly that, full run, check the orphans. Only then the flag.

The guard against that flag quietly rotting was never a ticket. It was that the status panel said,
on every page, in plain words: *"Scheduled guest purge is off — guest data is deleted only when
someone runs it by hand."* For six days this slice made the 24-hour promise *keepable* rather than
kept, and the UI was honest about which. Today it says `scheduled: true`, and that is honest too.

**Nothing in the code changed between those two states.** The difference was a backup, a dry run, a
number read rather than assumed, and a small deletion verified four ways before a large one. That
distinction is the whole slice, really. The code was the easy part.

One thing genuinely remains: beat's own hourly tick, due about an hour after the flag went on. Every
part of the path is proven — the entry is in the live schedule, the worker consumed the identical
message and wrote a heartbeat, the broker's bindings are clean — so what's left unproven is one
timer's arithmetic. Which is worth stating rather than rounding up to "done", because *"the only
untested part is the trivial part"* is how the last four bugs in this file introduced themselves.
