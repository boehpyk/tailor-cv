# The prompt eval corpus

This is what `make eval` (`python -m tailorcraft.cli eval-prompts`) runs against the **real** Gemini
API, by hand. It is not a test fixture: nothing in `make check` reads it, and a run costs money.
Prompt quality is a judgement call (ADR-0004), so the runner asserts only the cheap, objective things
and prints the documents for a person to read.

## Everything here is synthetic

**The CVs and the job postings were all written for this corpus.** Every person, company, school and
hospital named in them is made up. Any resemblance to a real one is a coincidence.

- **The CVs are synthetic because a real CV is PII** (AC-26). This repository is public, and a
  commit is permanent. Even with the name removed, a CV is someone's employment history, and removing
  it from the history of a public repository after the fact does not remove it from the clones.
- **The postings are synthetic because real job ads are third-party copyrighted text.** Committing
  them verbatim would publish someone else's writing permanently in a public repository.
- **What that costs.** The realism of the eval is approximate. These documents are cleaner and more
  consistent than real ones. They also say nothing about scraped-page noise (navigation chrome, cookie
  banners, duplicated boilerplate), which slice 1.2's fetcher tests cover. A prompt that does well here
  has passed a rehearsal, not the real thing. Read the results with that in mind.

When you add to the corpus, keep it synthetic:

- **Emails and web addresses** (profile links, portfolio sites) use reserved example domains
  (`example.com`, or a name ending `.example`). The loader rejects anything else.
- **Phone numbers** use the UK drama range (`07700 900000` to `07700 900999`). The loader cannot check
  that, so review it by eye.
- **Organisation names** should be invented and distinctive, not common words.

## Layout

```
corpus.toml       the manifest: every CV, every posting, and the pairs that join them
cvs/*.md          synthetic base CVs, as a person would write them
postings/*.md     synthetic job postings, as a company would publish them
```

`corpus.toml` declares, for each CV, **the candidate's name** as the CV states it (`candidate_name`,
without credentials such as ", RN") and **every organisation its text names** (employers, schools,
short forms such as "Brambleway" for "Brambleway Logistics"). The name is declared rather than read
off the CV's first line because that line carries credentials and varies in shape from CV to CV.
It also declares **the entries a tailored CV must keep** (`must_keep`): one list per employer and per
education institution, holding its accepted forms with the full name first, such as
`["Calloway Health Systems", "Calloway"]`. That is kept apart from `organisations` on purpose, because
that list carries short forms as separate names and may carry a client named in a bullet, which a
tailored CV is free to drop. For each posting it declares the
company and any short forms. Each `[[pair]]` joins one CV to one posting and says what a reader
should `watch_for`, which is usually the gap between what the posting asks for and what the CV
contains. A CV may appear in several pairs.

The CV files keep their line breaks so the report can show them side by side with the tailored CV.
The pipeline itself receives them whitespace-collapsed through `ExtractedText`, exactly as text
extracted from an uploaded PDF or DOCX arrives.

## What the loader checks before any call

Run `python -m tailorcraft.cli eval-prompts --validate-only`. It makes no API call and needs no key.
It checks that:

- every file exists and is used by a pair;
- every CV passes `ExtractedText` and fits `llm_max_cv_characters`;
- every posting passes `JobPostingText`;
- every declared name actually appears in its own text. A typo would otherwise switch the employer
  check off without a sound, or turn the name check upside down (every correct document failing, a
  document repeating the typo passing);
- every `must_keep` entry has at least one form its CV's text names (so a typo cannot make a faithful
  tailored CV fail), and no form names a form of another entry (so a deleted entry cannot pass on its
  neighbour's mention);
- no CV names another entry's organisation without declaring it, and no declared name contains
  another entry's name. Either would make a faithful tailored CV look like a fabrication;
- every email address and web address uses a reserved example domain.

That last check is the only machine check on this directory. The repository's pre-commit hook
guards `uploads/` and API keys, not `eval/`, so a real person's details that avoid an email or a web
address (a name, a phone number, an employer) are caught only by reading the diff.

## The employer check, and what it cannot see

For each pair, the runner flags any name that appears in the tailored CV's experience section (or in
the whole CV when no experience heading exists) and that belongs either to **the posting's own
company** or to **another corpus CV**, but not to the base CV.

- The posting's company is the realistic signal. It is the one employer the model has been told
  about.
- Another CV's employer can only appear if prompts or responses were mixed up, because the model
  never sees the other CVs. That half checks the harness more than the model.
- **An employer the model invents from nothing is invisible to it.** The report lists capitalised
  phrases the base CV never uses to help you look, but that list is not a check. Reading the
  documents is.

## The name check, and what it cannot see

The second paid eval headed pair 05's tailored CV **"TOMAZ REYES, RN"**. The base CV says "TOMASZ".
A person found it by reading, because no check looked at the name. Now two do: the declared
`candidate_name` must appear in the **tailored CV** and in the **cover letter**, each reported as its
own PASS or FAIL, so a FAIL says which document lacks it. Matching is the employer check's: whole
phrase, any case, tolerant of line breaks and repeated spaces. "Tomasz Reyes" and "TOMASZ REYES" pass;
"TOMAZ REYES" fails.

- **It proves the name is present somewhere, not that the header carries it.** A misspelled header
  above a correctly spelled name further down still passes. Read the header.
- **It does not catch an added middle name or a nickname once the declared name appears anywhere in
  the same document.** "Tomasz J. Reyes" as the only form of the name fails, because the phrase is
  broken. But a letter that opens "I am Tomasz Reyes" and is signed "Tom" passes, and so does a CV
  headed "TOMASZ J. REYES" that names "Tomasz Reyes" further down.
- **A person referred to only by initials needs a different rule.** "T. Reyes" fails here, which is
  right for this corpus. A candidate whose own CV uses initials would need the rule changed, not the
  declaration bent to fit.
- A name split by Markdown markup ("**Tomasz** Reyes") is missed, as for employers.

## The deleted-entry check, and what it cannot see

The third paid eval (prompt v3) returned pair 09's tailored CV naming only Calloway Health Systems
and Ferngate Clinical Software. Obsidian Lantern Games, Rookwood Telematics, Pelham Brothers Insurance
and both universities were simply gone, at 486 words against an 800-word cap and a prompt rule saying
older roles shrink to one line. An ad-hoc comparison found it, because no check looked. Now one does:
**every `must_keep` entry must be named in the tailored CV in at least one of its accepted forms.**
Matching is the other checks': whole phrase, any case, tolerant of line breaks. A FAIL lists every
missing entry by its first form and counts toward the exit code. With no documents it is skipped.

- **It proves each entry is present, not where.** It does not check that the entry sits in the
  experience or education section. A mention inside another bullet satisfies it, so a role deleted
  from the history but named in a career highlight still passes. Read the history.
- **It says nothing about the entry's title, qualification or dates.** The right employer on a line
  with the wrong year passes.
- **It matches institutions, not qualifications.** Two degrees from one university are one entry
  (Deanhollow University in `teacher-to-ux`), so dropping one of the two passes.
- A form that is misspelled, shortened beyond its declared forms, or split by Markdown markup counts
  as missing. That FAIL is worth reading, but it is not always a deletion.

## The pairs

| Pair | CV | Posting | Why it is here |
|---|---|---|---|
| 01 | senior backend engineer | Senior Backend Engineer, freight SaaS | strong match |
| 02 | senior backend engineer | Staff Platform Engineer, bank | Kubernetes / Go / staff-level gap |
| 03 | junior data analyst | Junior Data Analyst, retail | good junior match, short posting |
| 04 | junior data analyst | BI Analyst, healthcare | years / Tableau / dbt gap |
| 05 | staff nurse | ICU Registered Nurse | partial gap: ICU experience, ALS |
| 06 | staff nurse | Clinical Informatics Specialist | transferable, no formal informatics role |
| 07 | marketing manager | Head of Growth, consumer app | seniority stretch |
| 08 | marketing manager | Product Marketing Manager, B2B SaaS | industry stretch |
| 09 | director of engineering (long CV) | VP Engineering, healthtech (long posting) | the largest prompt; output-cap truncation risk |
| 10 | teacher moving into UX | UX Researcher, edtech | career change |

One run is ten model calls, and up to twenty if every run retries once (`llm_max_attempts = 2`). Use
`--only 03 --only 09` while iterating on the prompt, and run the whole corpus before merging a prompt
change.
