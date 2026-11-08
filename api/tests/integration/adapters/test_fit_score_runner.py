"""The fit-score measurement (`eval-fit-scores`, T43), offline, written **after** the code (T45).

The runner drives the real `GeminiFitScorer` through its `generate` seam, so every test here hands it
a scripted fake and no test can reach Gemini (ADR-0004). The fake recognises which corpus CV and
posting a prompt carries, then answers from a table the test wrote, so the expected numbers come from
the spec's arithmetic (AC-51's range of 5, AC-52's margin of 20, ADR-0041's rounding), not from the
runner's own output.

The spend guard is tested as a spy: with no `--confirm-spend`, or with it but no key, the function
that would reach the provider is **never called**. `asyncio.run` is swapped for identity there so a
sync test does not tear down pytest-asyncio's session loop.

Mutations proven red (T45, restored with `git checkout`): `MAX_SCORE_RANGE` 5 -> 6 fails the range
boundary; deleting the `if not confirm_spend` return fails the no-call guard.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest

from tailorcraft.domain.scoring.value_objects import CvForScoring
from tailorcraft.infrastructure.llm.evaluation import fit_score_runner
from tailorcraft.infrastructure.llm.evaluation.corpus import (
    CorpusError,
    FitScoreCorpus,
    load_fit_score_corpus,
)
from tailorcraft.infrastructure.llm.evaluation.fit_score_runner import (
    EXIT_CANNOT_START,
    EXIT_FAILED,
    EXIT_OK,
    plan_jobs,
    run_fit_score_eval,
)
from tailorcraft.infrastructure.llm.fit_scorer import PROMPT_OVERHEAD_CHARACTERS
from tailorcraft.infrastructure.llm.gemini import RawCompletion
from tailorcraft.infrastructure.scoring.draft_text import DraftTextAdapter
from tailorcraft.infrastructure.settings import Settings

API_DIR = Path(__file__).resolve().parents[3]
CORPUS_DIR = API_DIR / "eval" / "fit-score"
REPEAT = 5
GAP_SENTINEL = "SENTINEL-GAP-REQUIREMENT-7f3a"

Key = tuple[str, str]  # (cv id, posting id)
Behaviour = Callable[[Key, int], "int | str"]  # int: a score; str: the raw text to return


@pytest.fixture(scope="module")
def corpus() -> FitScoreCorpus:
    return load_fit_score_corpus(CORPUS_DIR, max_cv_characters=40_000)


def _cfg(settings: Settings, **overrides: object) -> Settings:
    update: dict[str, object] = {
        "gemini_api_key": "placeholder-test-key-never-a-real-secret",
        "fit_score_retry_backoff_seconds": 0.0,
        "fit_score_input_price_usd_per_million": Decimal("0.30"),
        "fit_score_output_price_usd_per_million": Decimal("2.50"),
        **overrides,
    }
    return settings.model_copy(update=update)


class FakeGenerate:
    """A `GenerateFn` that finds the corpus CV and posting in the prompt and answers from a table."""

    def __init__(
        self,
        corpus: FitScoreCorpus,
        behaviour: Behaviour,
        *,
        prompt_tokens: int = 1_000,
        completion_tokens: int = 100,
    ) -> None:
        cvs = {cv.id: cv.text.value for cv in corpus.base.cvs}
        postings = {p.id: p.text.value for p in corpus.base.postings}
        postings.update({i.posting.id: i.posting.text.value for i in corpus.injected})
        self._cvs, self._postings = cvs, postings
        self._behaviour = behaviour
        self._tokens = (prompt_tokens, completion_tokens)
        self.prompts: list[str] = []
        self.completions: list[str] = []
        self.per_key: Counter[Key] = Counter()

    def _find(self, table: dict[str, str], prompt: str) -> str:
        found = [key for key, text in table.items() if text in prompt]
        assert len(found) == 1, f"prompt did not carry exactly one corpus text: {found}"
        return found[0]

    async def __call__(self, prompt: str) -> RawCompletion:
        key = (self._find(self._cvs, prompt), self._find(self._postings, prompt))
        self.per_key[key] += 1
        answer = self._behaviour(key, self.per_key[key])
        if isinstance(answer, str):
            text = answer
        else:
            quote = " ".join(self._postings[key[1]].split()[:8])
            gap = {"requirement": GAP_SENTINEL, "posting_quote": quote}
            text = json.dumps({"score": answer, "gaps": [gap]})
        self.prompts.append(prompt)
        self.completions.append(text)
        return RawCompletion(
            text=text, prompt_tokens=self._tokens[0], completion_tokens=self._tokens[1]
        )

    @property
    def calls(self) -> int:
        return len(self.prompts)


def _separating(corpus: FitScoreCorpus, matched: int = 80, mismatched: int = 40) -> Behaviour:
    crosses = {(c.cv.id, c.posting.id) for c in corpus.cross}

    def behave(key: Key, n: int) -> int:
        return mismatched if key in crosses else matched

    return behave


async def _run(
    corpus: FitScoreCorpus, settings: Settings, fake: FakeGenerate, *, repeat: int = REPEAT
) -> tuple[int, str]:
    out = io.StringIO()
    code = await run_fit_score_eval(corpus, settings, out=out, repeat=repeat, generate=fake)
    return code, out.getvalue()


# -- AC-51 / AC-52: range and exit codes --------------------------------------------------------


async def test_a_stable_and_discriminating_scorer_exits_0_after_one_call_per_planned_score(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    fake = FakeGenerate(corpus, _separating(corpus))
    code, out = await _run(corpus, _cfg(settings), fake)
    assert code == EXIT_OK
    assert fake.calls == len(plan_jobs(corpus, REPEAT)) == 58
    assert "verdict: AC-51 PASS, AC-52 PASS" in out


@pytest.mark.parametrize(
    ("spread", "exit_code"),
    [(5, EXIT_OK), (6, EXIT_FAILED)],
    ids=["range-5-holds", "range-6-fails"],
)
async def test_ac51_allows_a_range_of_five_and_no_more(
    settings: Settings, corpus: FitScoreCorpus, spread: int, exit_code: int
) -> None:
    base = _separating(corpus)

    def behave(key: Key, n: int) -> int | str:
        # One matched pair (cv backend-senior x senior-backend-freight) swings by `spread`.
        swing = spread if key == ("backend-senior", "senior-backend-freight") and n == 1 else 0
        value = base(key, n)
        assert isinstance(value, int)
        return value - swing

    code, out = await _run(corpus, _cfg(settings), FakeGenerate(corpus, behave))
    assert code == exit_code
    assert ("AC-51 FAIL" in out) is (exit_code == EXIT_FAILED)


@pytest.mark.parametrize(
    ("margin", "exit_code"), [(20, EXIT_OK), (19, EXIT_FAILED)], ids=["margin-20-holds", "19-fails"]
)
async def test_ac52_needs_the_matched_median_twenty_points_above_the_mismatched_pair(
    settings: Settings, corpus: FitScoreCorpus, margin: int, exit_code: int
) -> None:
    code, out = await _run(
        corpus, _cfg(settings), FakeGenerate(corpus, _separating(corpus, 80, 80 - margin))
    )
    assert code == exit_code
    assert "AC-51 PASS" in out  # a steady scorer: only discrimination differs
    assert ("AC-52 FAIL" in out) is (exit_code == EXIT_FAILED)


async def test_a_scorer_answering_70_every_time_passes_ac51_and_fails_ac52(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    code, out = await _run(corpus, _cfg(settings), FakeGenerate(corpus, lambda key, n: 70))
    assert code == EXIT_FAILED
    assert "verdict: AC-51 PASS, AC-52 FAIL -- no release" in out


async def test_a_pair_whose_repeats_do_not_all_score_is_unstable_not_skipped(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    base = _separating(corpus)

    def behave(key: Key, n: int) -> int | str:
        return "not json at all" if key == ("nurse-midlevel", "icu-nurse") else base(key, n)

    code, out = await _run(corpus, _cfg(settings), FakeGenerate(corpus, behave))
    assert code == EXIT_FAILED
    assert f"only 0 of {REPEAT} scored FAIL" in out


# -- AC-53: cost arithmetic ---------------------------------------------------------------------


async def test_cost_per_score_is_metered_tokens_priced_and_rounded_up_each_way(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    # 1,000 in at $0.30/M = 300 micro-USD; 100 out at $2.50/M = 250: 550 per score, 58 scores.
    fake = FakeGenerate(corpus, _separating(corpus), prompt_tokens=1_000, completion_tokens=100)
    _, out = await _run(corpus, _cfg(settings), fake)
    assert "mean $0.000550, p95 $0.000550, max $0.000550, total $0.031900 over 58 scores" in out
    assert "above the PRD" not in out


async def test_a_mean_above_the_prd_target_is_flagged_for_the_owner(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    # 3,333 in = 999.9 rounded up to 1,000 micro-USD, plus 1 out token = 2.5 rounded up to 3:
    # 1,003 > the PRD's 1,000.
    fake = FakeGenerate(corpus, _separating(corpus), prompt_tokens=3_333, completion_tokens=1)
    _, out = await _run(corpus, _cfg(settings), fake)
    assert "mean $0.001003" in out
    assert "the mean is above the PRD's $0.001000: report it to the owner" in out


async def test_the_plan_upper_bound_prices_every_attempt_at_one_token_per_character(
    settings: Settings, corpus: FitScoreCorpus, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(settings)
    expected = 0
    for job in plan_jobs(corpus, REPEAT):
        tokens_in = (
            job.cv.character_count + job.posting.character_count + PROMPT_OVERHEAD_CHARACTERS
        )
        in_cost = (Decimal(tokens_in) * Decimal("0.30")).to_integral_value(ROUND_CEILING)
        out_cost = (Decimal(cfg.fit_score_max_output_tokens) * Decimal("2.50")).to_integral_value(
            ROUND_CEILING
        )
        expected += int(in_cost + out_cost) * cfg.fit_score_max_attempts
    out = io.StringIO()
    fit_score_runner.render_plan(out, plan_jobs(corpus, REPEAT), cfg, repeat=REPEAT)
    assert f"upper bound: ${Decimal(expected) / 1_000_000:.6f}" in out.getvalue()
    assert "58 scores, at most 116 calls" in out.getvalue()


# -- AC-54: parser verdicts counted per call ----------------------------------------------------


async def test_a_refusal_repaired_by_the_retry_is_still_counted_as_a_refusal(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    base = _separating(corpus)

    def behave(key: Key, n: int) -> int | str:
        if key == ("teacher-to-ux", "ux-researcher-edtech") and n == 1:
            return "I cannot help with that."
        return base(key, n)

    fake = FakeGenerate(corpus, behave)
    code, out = await _run(corpus, _cfg(settings), fake)
    assert code == EXIT_OK  # the retry repaired it: the score exists
    assert fake.calls == 59
    assert "AC-54 per call (59 calls)" in out
    assert "  not_json: 1 (1.7%)" in out
    assert "  parsed: 58 (98.3%)" in out
    assert "[not_json, parsed]" in out


async def test_a_refusal_rate_above_ten_percent_is_called_a_prompt_defect(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    base = _separating(corpus)

    def behave(key: Key, n: int) -> int | str:
        return (
            "nope" if n == 1 else base(key, n)
        )  # each key refused on its first call only: 18 keys

    _, out = await _run(corpus, _cfg(settings), FakeGenerate(corpus, behave))
    assert (
        "parser refusals 23.7% are above 10%: a prompt defect to fix before release" in out
    )  # 18 of 76


async def test_thoughts_tokens_reported_by_the_provider_fail_the_thinking_check(
    settings: Settings, corpus: FitScoreCorpus
) -> None:
    _, out = await _run(corpus, _cfg(settings), FakeGenerate(corpus, _separating(corpus)))
    assert "AC-54 thinking: 0 of 58 returned calls reported thoughts PASS" in out


# -- the output never carries text --------------------------------------------------------------


async def test_the_output_never_contains_a_gap_a_quote_a_prompt_or_a_completion(
    settings: Settings, corpus: FitScoreCorpus, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeGenerate(corpus, _separating(corpus))
    with caplog.at_level(0):
        _, out = await _run(corpus, _cfg(settings), fake)
    quotes = {" ".join(p.text.value.split()[:8]) for p in corpus.base.postings}
    forbidden = {GAP_SENTINEL, *quotes}
    forbidden |= {cv.text.value[:60] for cv in corpus.base.cvs}
    forbidden |= {p.text.value[:60] for p in corpus.base.postings}
    forbidden |= {prompt[:200] for prompt in fake.prompts} | set(fake.completions)
    for text in forbidden:
        assert text not in out
        assert text not in caplog.text


# -- the CLI entry: no call without --confirm-spend ---------------------------------------------


class _Spy:
    """Stands in for `run_fit_score_eval`; `asyncio.run` is identity, so a sync test can call it."""

    def __init__(self, code: int = 0) -> None:
        self.calls = 0
        self.code = code

    def __call__(self, *args: object, **kwargs: object) -> int:
        self.calls += 1
        return self.code


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Callable[..., tuple[int, _Spy]]:
    def run(
        *, key: str = "placeholder-key", spy_code: int = 0, **kwargs: object
    ) -> tuple[int, _Spy]:
        spy = _Spy(spy_code)
        monkeypatch.setattr(
            fit_score_runner, "get_settings", lambda: _cfg(settings, gemini_api_key=key)
        )
        monkeypatch.setattr(fit_score_runner, "configure_logging", lambda s: None)
        monkeypatch.setattr(fit_score_runner, "route_logs_to_stderr", lambda: None)
        monkeypatch.setattr(fit_score_runner, "run_fit_score_eval", spy)
        monkeypatch.setattr(asyncio, "run", lambda coro_or_code: coro_or_code)
        options: dict[str, object] = {
            "corpus_dir": CORPUS_DIR,
            "repeat": REPEAT,
            "confirm_spend": False,
            "validate_only": False,
            **kwargs,
        }
        return fit_score_runner.run_from_cli(**options), spy  # type: ignore[arg-type]

    return run


def test_without_confirm_spend_it_prints_the_plan_and_makes_no_call(
    cli: Callable[..., tuple[int, _Spy]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, spy = cli()
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert spy.calls == 0
    assert "upper bound: $" in out
    assert "No call was made. Re-run with --confirm-spend" in out


def test_without_confirm_spend_a_missing_key_does_not_matter(
    cli: Callable[..., tuple[int, _Spy]],
) -> None:
    code, spy = cli(key="")
    assert (code, spy.calls) == (EXIT_OK, 0)


def test_validate_only_makes_no_call_even_when_confirm_spend_is_given(
    cli: Callable[..., tuple[int, _Spy]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, spy = cli(validate_only=True, confirm_spend=True)
    assert (code, spy.calls) == (EXIT_OK, 0)
    assert "corpus is valid; no API call was made." in capsys.readouterr().out


def test_confirm_spend_with_no_key_exits_2_before_any_call(
    cli: Callable[..., tuple[int, _Spy]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, spy = cli(key="   ", confirm_spend=True)
    assert (code, spy.calls) == (EXIT_CANNOT_START, 0)
    assert "GEMINI_API_KEY is empty" in capsys.readouterr().err


@pytest.mark.parametrize("measured", [EXIT_OK, EXIT_FAILED])
def test_confirm_spend_with_a_key_runs_once_and_returns_the_measurement_exit_code(
    cli: Callable[..., tuple[int, _Spy]], measured: int
) -> None:
    code, spy = cli(confirm_spend=True, spy_code=measured)
    assert (code, spy.calls) == (measured, 1)


@pytest.mark.parametrize("repeat", [1, 0, -3])
def test_a_repeat_under_two_cannot_measure_stability_and_exits_2(
    cli: Callable[..., tuple[int, _Spy]], repeat: int, capsys: pytest.CaptureFixture[str]
) -> None:
    code, spy = cli(repeat=repeat, confirm_spend=True)
    assert (code, spy.calls) == (EXIT_CANNOT_START, 0)
    assert "--repeat must be at least 2" in capsys.readouterr().err


def test_a_missing_corpus_directory_exits_2(
    cli: Callable[..., tuple[int, _Spy]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, spy = cli(corpus_dir=tmp_path / "nowhere", confirm_spend=True)
    assert (code, spy.calls) == (EXIT_CANNOT_START, 0)
    assert "no corpus.toml" in capsys.readouterr().err


# -- the corpus loader's refusals ---------------------------------------------------------------


@pytest.fixture
def corpus_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the fit-score corpus beside the base corpus it references."""
    shutil.copytree(API_DIR / "eval" / "corpus", tmp_path / "corpus")
    shutil.copytree(CORPUS_DIR, tmp_path / "fit-score")
    return tmp_path / "fit-score"


def _edit(root: Path, old: str, new: str) -> None:
    manifest = root / "corpus.toml"
    text = manifest.read_text(encoding="utf-8")
    assert old in text, "the edit must change the manifest, or the test proves nothing"
    manifest.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_an_untouched_copy_loads(corpus_copy: Path) -> None:
    loaded = load_fit_score_corpus(corpus_copy, max_cv_characters=40_000)
    assert len(loaded.cross) == 6
    assert len(loaded.injected) == 2


_CROSS_BACKEND = '[[cross]]\ncv = "backend-senior"\nposting = "icu-nurse"\nmatched_pair = "01"\n'


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (_CROSS_BACKEND, "", "every base CV needs one [[cross]] pair"),
        (
            'posting = "icu-nurse"\nmatched_pair = "01"',
            'posting = "senior-backend-freight"\nmatched_pair = "01"',
            "is one of this CV's own pairs, not a mismatch",
        ),
        ('matched_pair = "01"', 'matched_pair = "05"', "must name one of this CV's own pairs"),
        ('matched_pair = "01"', 'matched_pair = "99"', "must name one of this CV's own pairs"),
        (
            'cv = "backend-senior"\nposting = "icu-nurse"',
            'cv = "ghost"\nposting = "icu-nurse"',
            "no [[cv]] with id `ghost`",
        ),
        (
            'posting = "icu-nurse"\nmatched_pair = "01"',
            'posting = "ghost"\nmatched_pair = "01"',
            "no [[posting]] with id `ghost`",
        ),
        ('cv = "teacher-to-ux"\ntries', 'cv = "ghost"\ntries', "no [[cv]] with id `ghost`"),
        ("\n# -- injected pairs", "\n" + _CROSS_BACKEND + "\n# -- injected pairs", "duplicate"),
        ('base_corpus = "../corpus"', 'base_corpus = "../absent"', "absent"),
        ('base_corpus = "../corpus"', "base_corpus = [", "not valid TOML"),
    ],
    ids=[
        "cv-without-a-cross-pair",
        "cross-posting-is-an-own-pair",
        "matched-pair-of-another-cv",
        "matched-pair-unknown",
        "cross-cv-unknown",
        "cross-posting-unknown",
        "injected-cv-unknown",
        "duplicate-cross",
        "base-corpus-missing",
        "invalid-toml",
    ],
)
def test_the_loader_refuses_a_malformed_manifest(
    corpus_copy: Path, old: str, new: str, message: str
) -> None:
    _edit(corpus_copy, old, new)
    with pytest.raises(
        CorpusError, match=message.replace("[", r"\[").replace("]", r"\]")
    ) as caught:
        load_fit_score_corpus(corpus_copy, max_cv_characters=40_000)
    assert caught.value.__cause__ is None or isinstance(caught.value.__cause__, Exception)


def test_the_loader_refuses_a_directory_with_no_manifest(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match=r"no corpus\.toml"):
        load_fit_score_corpus(tmp_path, max_cv_characters=40_000)


def test_the_loader_refuses_an_injected_posting_file_that_does_not_exist(corpus_copy: Path) -> None:
    (corpus_copy / "postings" / "injected-score-override.md").unlink()
    with pytest.raises(CorpusError, match="does not exist"):
        load_fit_score_corpus(corpus_copy, max_cv_characters=40_000)


# -- AC-24 (5.2, T39): `--drafts` scores `make eval`'s stored drafts -------------------------------


def _write_drafts(corpus: FitScoreCorpus, root: Path, count: int) -> list[str]:
    """The first `count` base pairs get a draft: their CV's own Markdown stands in for one."""
    root.mkdir(parents=True, exist_ok=True)
    ids = [pair.id for pair in corpus.base.pairs[:count]]
    for pair in corpus.base.pairs[:count]:
        (root / f"{pair.id}.md").write_text(f"# Draft\n\n{pair.cv.raw_text}", encoding="utf-8")
    return ids


async def test_load_drafts_converts_each_through_draft_text_adapter_and_names_the_missing(
    corpus: FitScoreCorpus, tmp_path: Path
) -> None:
    ids = _write_drafts(corpus, tmp_path, 2)
    drafts, missing = await fit_score_runner.load_drafts(tmp_path, corpus)
    assert [d.pair_id for d in drafts] == ids
    assert list(missing) == [p.id for p in corpus.base.pairs[2:]]
    expected = await DraftTextAdapter().to_plain_text((tmp_path / f"{ids[0]}.md").read_text())
    assert drafts[0].cv.value == CvForScoring(expected).value
    assert not drafts[0].cv.value.startswith("#"), "the TXT pipeline drops the heading marker"
    assert drafts[0].posting.value == corpus.base.pairs[0].posting.text.value


async def test_a_blank_draft_is_refused_as_a_corpus_error(
    corpus: FitScoreCorpus, tmp_path: Path
) -> None:
    (tmp_path / f"{corpus.base.pairs[0].id}.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(CorpusError, match="blank as plain text"):
        await fit_score_runner.load_drafts(tmp_path, corpus)


@pytest.mark.parametrize(
    ("spread", "exit_code"), [(5, EXIT_OK), (6, EXIT_FAILED)], ids=["range-5", "range-6"]
)
async def test_ac24_scores_each_draft_repeat_times_and_allows_a_range_of_five(
    settings: Settings, corpus: FitScoreCorpus, tmp_path: Path, spread: int, exit_code: int
) -> None:
    _write_drafts(corpus, tmp_path, 2)
    drafts, _ = await fit_score_runner.load_drafts(tmp_path, corpus)
    calls = 0

    async def generate(prompt: str) -> RawCompletion:
        nonlocal calls
        calls += 1
        posting = next(d.posting.value for d in drafts if d.posting.value in prompt)
        score = 70 + (spread if calls % REPEAT == 0 else 0)
        quote = " ".join(posting.split()[:8])
        text = json.dumps(
            {"score": score, "gaps": [{"requirement": GAP_SENTINEL, "posting_quote": quote}]}
        )
        return RawCompletion(text=text, prompt_tokens=1_000, completion_tokens=100)

    out = io.StringIO()
    code = await run_fit_score_eval(
        corpus, _cfg(settings), out=out, repeat=REPEAT, generate=generate, drafts=drafts
    )
    assert (code, calls) == (exit_code, 2 * REPEAT)
    assert f"range {spread}" in out.getvalue()
    assert f"verdict: AC-24 {'PASS' if exit_code == EXIT_OK else 'FAIL'}" in out.getvalue()
    assert "AC-51" not in out.getvalue()


@pytest.fixture
def drafts_cli(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[Callable[..., tuple[int, list[object]]]]:
    """`run_from_cli` with `--drafts`. Loading the drafts is a real coroutine, so `asyncio.run` runs
    on a private loop (never the session's), and the measurement is an async spy."""
    loop = asyncio.new_event_loop()

    def run(*, key: str = "placeholder-key", **kwargs: object) -> tuple[int, list[object]]:
        seen: list[object] = []

        async def spy(*args: object, drafts: object = None, **_: object) -> int:
            seen.append(drafts)
            return EXIT_OK

        monkeypatch.setattr(
            fit_score_runner, "get_settings", lambda: _cfg(settings, gemini_api_key=key)
        )
        monkeypatch.setattr(fit_score_runner, "configure_logging", lambda s: None)
        monkeypatch.setattr(fit_score_runner, "route_logs_to_stderr", lambda: None)
        monkeypatch.setattr(fit_score_runner, "run_fit_score_eval", spy)
        monkeypatch.setattr(asyncio, "run", loop.run_until_complete)
        options: dict[str, object] = {
            "corpus_dir": CORPUS_DIR,
            "repeat": REPEAT,
            "confirm_spend": False,
            "validate_only": False,
            **kwargs,
        }
        return fit_score_runner.run_from_cli(**options), seen  # type: ignore[arg-type]

    yield run
    loop.close()


def test_drafts_without_confirm_spend_print_the_plan_and_make_no_call(
    drafts_cli: Callable[..., tuple[int, list[object]]],
    corpus: FitScoreCorpus,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_drafts(corpus, tmp_path, 3)
    code, seen = drafts_cli(drafts_dir=tmp_path, key="")
    out = capsys.readouterr().out
    assert (code, seen) == (EXIT_OK, [])
    assert "drafts: 3 from" in out
    assert "plan: 3 drafts x 5 repeats: 15 scores, at most 30 calls" in out
    assert "upper bound: $" in out
    assert "No call was made. Re-run with --confirm-spend" in out


def test_drafts_with_no_draft_on_disk_exit_2_and_say_what_is_needed(
    drafts_cli: Callable[..., tuple[int, list[object]]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, seen = drafts_cli(drafts_dir=tmp_path / "nowhere", confirm_spend=True)
    assert (code, seen) == (EXIT_CANNOT_START, [])
    assert "no drafts in" in capsys.readouterr().err


def test_drafts_with_confirm_spend_and_a_key_measure_once_with_the_drafts(
    drafts_cli: Callable[..., tuple[int, list[object]]], corpus: FitScoreCorpus, tmp_path: Path
) -> None:
    ids = _write_drafts(corpus, tmp_path, 2)
    code, seen = drafts_cli(drafts_dir=tmp_path, confirm_spend=True)
    assert code == EXIT_OK
    assert len(seen) == 1
    assert [d.pair_id for d in seen[0]] == ids  # type: ignore[attr-defined]
