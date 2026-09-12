"""The eval corpus on disk: one manifest, synthetic CVs, synthetic job postings, and the pairs that
join them — loaded and validated **before a single paid call is made**.

A corpus mistake found after ten API calls has cost ten API calls, so everything that can be checked
offline is checked here, and `eval-prompts --validate-only` runs exactly this and nothing else:

- every file named in `corpus.toml` exists, stays inside the corpus directory, and is used by a pair;
- every CV passes `ExtractedText` and fits `llm_max_cv_characters` (a CV the adapter would refuse
  before calling measures nothing); every posting passes `JobPostingText`;
- **every declared organisation actually appears in its own text** — a typo in the manifest would
  otherwise blind the employer check without a sound;
- **every CV's declared `candidate_name` actually appears in its own text** — the same guard for the
  name-fidelity check, where a typo is worse than blinding: a manifest saying "Tomaz" would make every
  correctly spelled document FAIL and a document repeating the typo PASS;
- **no CV's text names another entry's organisation or company without declaring it**, and no
  declared name contains another entry's name — either would make a faithful tailored CV look like a
  fabrication;
- every email address **and every web address** uses a reserved example domain (RFC 2606 / RFC 6761).
  The corpus is synthetic and the repository is public (AC-26); a real-looking address is how a real
  one slips in, and a profile link (`linkedin.com/in/…`, a personal site) is the contact detail a CV
  carries most often after an email. Phone numbers are not checked — there is no reliable offline
  test for "fictional" across formats — and the corpus README asks for the reserved drama ranges
  instead.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.llm.evaluation.checks import mentions

MANIFEST_NAME: Final = "corpus.toml"

_IDENTIFIER: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# A bare hostname, as a CV writes a profile link: dot-separated labels ending in an alphabetic TLD.
# Scanned only AFTER email addresses are blanked out, or an address's local part ("idris.achterberg")
# would be read as a host. Requiring letters in the last label keeps "£1.8 million" and "St. Aubyn"
# (a space after the dot) out of it.
_HOSTNAME: Final = re.compile(
    r"(?<![\w@.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?![\w-])"
)
_RESERVED_DOMAINS: Final = frozenset({"example.com", "example.org", "example.net"})
_RESERVED_SUFFIXES: Final = (
    ".example",
    ".test",
    ".invalid",
    ".example.com",
    ".example.org",
    ".example.net",
)


class CorpusError(Exception):
    """The corpus on disk is not one the eval can run honestly. Always raised before any API call."""


@dataclass(frozen=True, slots=True)
class CorpusCv:
    id: str
    # As committed, line breaks intact: what the human reads beside the tailored CV.
    raw_text: str
    # As the pipeline sees it: whitespace-collapsed, exactly as extraction from a PDF or DOCX yields.
    text: ExtractedText
    # The candidate's name as the CV states it, declared rather than read off the first line: that line
    # carries credentials (", RN") and its shape varies from CV to CV.
    candidate_name: str
    organisations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusPosting:
    id: str
    raw_text: str
    text: JobPostingText
    title: str
    company: str
    aliases: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return (self.company, *self.aliases)


@dataclass(frozen=True, slots=True)
class CorpusPair:
    id: str
    cv: CorpusCv
    posting: CorpusPosting
    # What a human should look for in this pair — usually the gap between posting and CV.
    watch_for: str


@dataclass(frozen=True, slots=True)
class Corpus:
    root: Path
    cvs: tuple[CorpusCv, ...]
    postings: tuple[CorpusPosting, ...]
    pairs: tuple[CorpusPair, ...]

    @property
    def all_organisations(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for cv in self.cvs for name in cv.organisations))

    def select(self, only: Sequence[str] | None) -> tuple[CorpusPair, ...]:
        """The pairs to run: all of them, or the named ones in the order given (`--only`)."""
        if not only:
            return self.pairs
        by_id = {pair.id: pair for pair in self.pairs}
        unknown = [pair_id for pair_id in only if pair_id not in by_id]
        if unknown:
            raise CorpusError(
                f"unknown pair id(s): {', '.join(unknown)}; the corpus has {', '.join(by_id)}"
            )
        return tuple(by_id[pair_id] for pair_id in dict.fromkeys(only))


def load_corpus(root: Path, *, max_cv_characters: int) -> Corpus:
    """Load `root/corpus.toml` and everything it names, or raise `CorpusError` saying what is wrong."""
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise CorpusError(
            f"no {MANIFEST_NAME} in {root.resolve()} — run from api/ or pass --corpus <directory>"
        )
    try:
        with manifest_path.open("rb") as handle:
            manifest = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise CorpusError(f"{MANIFEST_NAME} is not valid TOML: {error}") from None

    cvs = tuple(_load_cv(root, table, max_cv_characters) for table in _tables(manifest, "cv"))
    postings = tuple(_load_posting(root, table) for table in _tables(manifest, "posting"))
    _require_unique("cv", [cv.id for cv in cvs])
    _require_unique("posting", [posting.id for posting in postings])
    pairs = _load_pairs(manifest, cvs, postings)
    _require_every_entry_used(cvs, postings, pairs)
    _require_unambiguous_names(cvs, postings)
    return Corpus(root=root, cvs=cvs, postings=postings, pairs=pairs)


# -- entries ---------------------------------------------------------------------------------------


def _load_cv(root: Path, table: dict[str, object], max_cv_characters: int) -> CorpusCv:
    cv_id = _identifier(table, "cv")
    where = f"cv `{cv_id}`"
    raw = _read(root, table, where)
    try:
        text = ExtractedText(raw)
    except DomainError as error:
        raise CorpusError(f"{where}: not a valid ExtractedText ({type(error).__name__})") from None
    if text.character_count > max_cv_characters:
        raise CorpusError(
            f"{where}: {text.character_count:,} characters exceeds llm_max_cv_characters "
            f"({max_cv_characters:,}); the adapter would refuse it before calling, so the pair "
            "would measure nothing"
        )
    candidate_name = " ".join(_string(table, "candidate_name", where).split())
    _require_named_in_text((candidate_name,), raw, where, check="the name-fidelity check")
    organisations = _strings(table, "organisations", where)
    _require_named_in_text(organisations, raw, where, check="the employer check")
    return CorpusCv(
        id=cv_id,
        raw_text=raw,
        text=text,
        candidate_name=candidate_name,
        organisations=organisations,
    )


def _load_posting(root: Path, table: dict[str, object]) -> CorpusPosting:
    posting_id = _identifier(table, "posting")
    where = f"posting `{posting_id}`"
    raw = _read(root, table, where)
    try:
        text = JobPostingText(raw)
    except DomainError as error:
        raise CorpusError(f"{where}: not a valid JobPostingText ({type(error).__name__})") from None
    posting = CorpusPosting(
        id=posting_id,
        raw_text=raw,
        text=text,
        title=_string(table, "title", where),
        company=_string(table, "company", where),
        aliases=_strings(table, "aliases", where, required=False),
    )
    _require_named_in_text(posting.names, raw, where, check="the employer check")
    return posting


def _load_pairs(
    manifest: dict[str, object], cvs: tuple[CorpusCv, ...], postings: tuple[CorpusPosting, ...]
) -> tuple[CorpusPair, ...]:
    cv_by_id = {cv.id: cv for cv in cvs}
    posting_by_id = {posting.id: posting for posting in postings}
    pairs: list[CorpusPair] = []
    for table in _tables(manifest, "pair"):
        pair_id = _identifier(table, "pair")
        where = f"pair `{pair_id}`"
        cv_id = _string(table, "cv", where)
        posting_id = _string(table, "posting", where)
        if cv_id not in cv_by_id:
            raise CorpusError(f"{where}: no [[cv]] with id `{cv_id}`")
        if posting_id not in posting_by_id:
            raise CorpusError(f"{where}: no [[posting]] with id `{posting_id}`")
        watch_for = table.get("watch_for", "")
        if not isinstance(watch_for, str):
            raise CorpusError(f"{where}: `watch_for` must be a string")
        pairs.append(
            CorpusPair(
                id=pair_id,
                cv=cv_by_id[cv_id],
                posting=posting_by_id[posting_id],
                watch_for=" ".join(watch_for.split()),
            )
        )
    _require_unique("pair", [pair.id for pair in pairs])
    return tuple(pairs)


# -- corpus-wide rules -----------------------------------------------------------------------------


def _require_every_entry_used(
    cvs: tuple[CorpusCv, ...], postings: tuple[CorpusPosting, ...], pairs: tuple[CorpusPair, ...]
) -> None:
    """A file no pair uses is a file nobody evaluates — and a name the employer check still counts."""
    unused_cvs = sorted({cv.id for cv in cvs} - {pair.cv.id for pair in pairs})
    unused_postings = sorted({p.id for p in postings} - {pair.posting.id for pair in pairs})
    if unused_cvs or unused_postings:
        raise CorpusError(
            "every entry must be used by a pair; unused "
            f"cvs: {unused_cvs or 'none'}, postings: {unused_postings or 'none'}"
        )


def _require_unambiguous_names(
    cvs: tuple[CorpusCv, ...], postings: tuple[CorpusPosting, ...]
) -> None:
    """Keep the employer check from flagging a document that did nothing wrong.

    Two ways it could: a CV whose own text names another entry's organisation (a faithful copy would
    then be "foreign"), and a declared name containing another entry's name ("Corvid Payments" would
    count as a mention of a posting company called "Corvid").
    """
    entities: list[tuple[str, tuple[str, ...]]] = [
        (f"cv `{cv.id}`", cv.organisations) for cv in cvs
    ] + [(f"posting `{posting.id}`", posting.names) for posting in postings]

    for owner, names in entities:
        own = {name.casefold() for name in names}
        foreign = {
            name
            for other_owner, other_names in entities
            if other_owner != owner
            for name in other_names
            if name.casefold() not in own
        }
        for name in names:
            for other in sorted(foreign):
                if mentions(name, other):
                    raise CorpusError(
                        f"{owner}: `{name}` contains `{other}`, which belongs to another corpus "
                        "entry, so a mention of one would be counted as the other"
                    )

    for cv in cvs:
        owner = f"cv `{cv.id}`"
        own = {name.casefold() for name in cv.organisations}
        named = sorted(
            {
                name
                for other_owner, other_names in entities
                if other_owner != owner
                for name in other_names
                if name.casefold() not in own and mentions(cv.raw_text, name)
            }
        )
        if named:
            raise CorpusError(
                f"{owner}: its text names {', '.join(named)}, which belong to other corpus "
                "entries; declare them in its `organisations` or rename them"
            )


def _require_named_in_text(names: tuple[str, ...], text: str, where: str, *, check: str) -> None:
    """Matched with `mentions`, the same function the checks use, so "appears" means the same here as
    it does when a tailored document is judged."""
    for name in names:
        if not mentions(text, name):
            raise CorpusError(
                f"{where}: declares `{name}` but its text never names it — a typo here "
                f"corrupts {check}"
            )


def _require_unique(kind: str, identifiers: list[str]) -> None:
    duplicates = sorted({i for i in identifiers if identifiers.count(i) > 1})
    if duplicates:
        raise CorpusError(f"duplicate [[{kind}]] id(s): {', '.join(duplicates)}")


def _is_reserved_domain(domain: str) -> bool:
    folded = domain.casefold()
    return folded in _RESERVED_DOMAINS or folded.endswith(_RESERVED_SUFFIXES)


def _require_reserved_domains(text: str, where: str) -> None:
    for match in _EMAIL.finditer(text):
        domain = match.group(1)
        if _is_reserved_domain(domain):
            continue
        # The domain only, never the address: if a real one did slip in, this message must not be
        # the thing that repeats it into a terminal scrollback or a CI log.
        raise CorpusError(
            f"{where}: an email address uses `{domain.casefold()}`, which is not a reserved example "
            "domain (example.com/.org/.net, or a .example/.test/.invalid name). The corpus is "
            "synthetic and public (AC-26)."
        )
    without_emails = _EMAIL.sub(" ", text)
    for line_number, line in enumerate(without_emails.splitlines(), start=1):
        for match in _HOSTNAME.finditer(line):
            if _is_reserved_domain(match.group(0)):
                continue
            # The line and the top-level label only. Unlike an email's domain, a hostname can BE the
            # contact detail (a personal site), so the message must not repeat it.
            tld = match.group(0).rsplit(".", 1)[-1].casefold()
            raise CorpusError(
                f"{where}: line {line_number} has a web address ending `.{tld}`, which is not a "
                "reserved example domain (use a .example name). The corpus is synthetic and "
                "public (AC-26)."
            )


# -- manifest parsing ------------------------------------------------------------------------------


def _tables(manifest: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = manifest.get(key)
    if not isinstance(raw, list) or not raw:
        raise CorpusError(f"{MANIFEST_NAME}: needs at least one [[{key}]] table")
    tables: list[dict[str, object]] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise CorpusError(f"{MANIFEST_NAME}: [[{key}]] number {position} is not a table")
        tables.append(item)
    return tables


def _identifier(table: dict[str, object], kind: str) -> str:
    value = _string(table, "id", f"a [[{kind}]] table")
    if _IDENTIFIER.fullmatch(value) is None:
        raise CorpusError(f"[[{kind}]] id `{value}` must be lowercase letters, digits and hyphens")
    return value


def _string(table: dict[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{where}: `{key}` must be a non-empty string")
    return value.strip()


def _strings(
    table: dict[str, object], key: str, where: str, *, required: bool = True
) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise CorpusError(f"{where}: `{key}` must be a list of non-empty strings")
    if required and not value:
        raise CorpusError(f"{where}: `{key}` must name at least one entry")
    return tuple(" ".join(v.split()) for v in value)


def _read(root: Path, table: dict[str, object], where: str) -> str:
    relative = _string(table, "file", where)
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CorpusError(f"{where}: `file` must stay inside the corpus directory")
    if not path.is_file():
        raise CorpusError(f"{where}: {relative} does not exist")
    text = path.read_text(encoding="utf-8")
    _require_reserved_domains(text, where)
    return text
