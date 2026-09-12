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

`corpus.toml` declares, for each CV, **every organisation its text names** (employers, schools,
short forms such as "Brambleway" for "Brambleway Logistics"). For each posting it declares the
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
  check off without a sound;
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
