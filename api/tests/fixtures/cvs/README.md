# CV fixture corpus

Committed test fixtures for `intake-base-cv-upload` (T25). Generated once, deterministically, by a
throwaway script run inside the `api` container (`pypdf` + `python-docx`, no `reportlab`/`PIL`
needed — the PDFs are built by hand from raw PDF objects). Nothing here is a real person's data.

This corpus is also the seed of PRD §10 validation 1 (20 varied CV layouts): **it grows over time,
it does not get replaced.** If you add a fixture, add a row below and say which failure-contract row
(`docs/specs/intake-base-cv-upload/feature-spec.md`) it exists to exercise.

| File | What it is | Exercises |
|---|---|---|
| `sample.pdf` | A one-page PDF with a real text layer (~870 non-whitespace characters), built from hand-written PDF content-stream operators (`BT`/`Td`/`Tj`) against the standard Helvetica font — no embedded font needed. | AC-1 (PDF happy path), F-6 |
| `sample.docx` | The same CV body as `sample.pdf`, as DOCX paragraphs. | AC-1 (DOCX happy path) |
| `sample.txt` | The same CV body as plain UTF-8 text. | AC-1 (TXT happy path), AC-1's exact `character_count` |
| `scanned.pdf` | A one-page PDF carrying an image XObject (4×4 raw, uncompressed `DeviceRGB` pixels) and **no text content stream at all** — `pypdf`'s `extract_text()` returns `""`, the same structural shape as a real scan with no OCR text layer. | F-9 (`no_text_layer`) |
| `encrypted.pdf` | `sample.pdf`, re-written through `pypdf.PdfWriter.encrypt()` with a user password. `is_encrypted` is `True`; `extract_text()` raises `FileNotDecryptedError`. | F-7 (`encrypted`) |
| `corrupt.pdf` | `sample.pdf`'s bytes truncated to half length — `%PDF-` still matches at offset 0 (so it still sniffs as PDF), but the xref/trailer is gone and `pypdf` raises `PdfReadError`. | F-8 (`corrupt`) |
| `not-a-pdf.pdf` | A minimal, genuinely valid 1×1 grayscale PNG (hand-built: signature + `IHDR` + `IDAT` + `IEND`, real CRC32s), saved with a `.pdf` extension. `sniff_cv_content_type` correctly returns `None` for it regardless of the filename or a declared `Content-Type: application/pdf`. | AC-4 (sniffing wins over both the filename and the client's `Content-Type`), F-5 |
| `tiny.txt` | ~50 characters, well under `ExtractedText`'s 200-character floor. | F-10 (`too_short`) |

## Fixtures generated in the test itself, not committed here

Per `technical-plan.md`'s test plan, these are built on the fly rather than stored:

- **The 11 MB oversized body** (F-3/AC-2) — a multi-megabyte binary committed to the repo for one
  size assertion is not worth the repo weight.
- **A >50-page PDF** (F-11) — built from blank pages (`pypdf.PdfWriter.add_blank_page`) in
  `tests/api/test_intake.py`; the page-count cap is checked before any page's content is parsed
  (ADR-0009 §2), so a blank page is exactly as effective as a real one for this row.
- **Bytes that decode as neither UTF-8 nor cp1252** (F-13) — a short literal (`b"\x81" * 32`) inline
  in the test; no reason for a fixture file.
- **A bare ZIP and an XLSX-shaped ZIP** (F-4/AC-3) — built with `zipfile` inline in the test, to
  prove `sniff_cv_content_type`'s namelist check (not just the magic bytes) is what rejects them.

## Regenerating this corpus

There is no committed generator script (it was a one-off, run interactively). To regenerate: build a
one-page PDF from PDF content-stream operators against `pypdf.PdfWriter` (no `reportlab` dependency),
mirror the same paragraph list into a `python-docx` `Document` and a plain `.txt`, then derive
`encrypted.pdf` (`PdfWriter.encrypt`), `corrupt.pdf` (truncate `sample.pdf`), `scanned.pdf` (a blank
page plus a raw image `XObject`, no text operator), and `not-a-pdf.pdf` (a hand-built PNG) from
`sample.pdf`. Keep every file small — these are fixtures, not fixtures-that-double-as-load-tests.

**If you regenerate `sample.pdf`/`sample.docx`/`sample.txt` with different content**, update the
fragments `tests/api/test_intake.py`'s privacy test (AC-12) asserts are absent from the logs
(`_SAMPLE_CV_NAME_FRAGMENT`, `_SAMPLE_CV_EMAIL_FRAGMENT`, `_SAMPLE_CV_EMPLOYER_FRAGMENT`) to match.
