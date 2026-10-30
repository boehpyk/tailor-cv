"""RED (T5): the `scoring` value objects, from the spec (AC-25, AC-27), ADR-0040/0041 and the plan's
"Corrections made during implementation" (prices are micro-USD per million tokens). Pure: no I/O."""

from __future__ import annotations

import hashlib

import pytest

from tailorcraft.domain.scoring.errors import (
    InvalidFingerprint,
    InvalidFitAssessment,
    InvalidFitGap,
    InvalidMicroUsd,
    InvalidScorePercent,
    InvalidScoringPrices,
    InvalidScoringText,
)
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    CvForScoring,
    FitAssessment,
    FitGap,
    MicroUsd,
    ModelName,
    PostingFingerprint,
    PostingForScoring,
    PromptVersion,
    ScorePercent,
    ScoringPrices,
    ScoringUsage,
)

SECRET = "Jane Doe, 12 Privet Drive"


def gap(text: str = "Kubernetes experience") -> FitGap:
    return FitGap(text)


def usage(input_tokens: int = 0, output_tokens: int = 0, unmetered: int = 0) -> ScoringUsage:
    return ScoringUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unmetered_attempts=unmetered,
        duration_ms=10,
        model=ModelName("gemini-x"),
        prompt_version=PromptVersion("v1"),
    )


def prices(inp: int = 100_000, out: int = 400_000) -> ScoringPrices:
    return ScoringPrices(MicroUsd(inp), MicroUsd(out))


# --- normalization and fingerprints -------------------------------------------------------------


@pytest.mark.parametrize("cls", [CvForScoring, PostingForScoring])
def test_whitespace_runs_collapse_and_ends_are_trimmed(cls: type) -> None:
    assert cls("  Led   a\t team \n\n of  five  ").value == "Led a team of five"


@pytest.mark.parametrize("cls", [CvForScoring, PostingForScoring])
def test_text_differing_only_in_whitespace_has_one_fingerprint(cls: type) -> None:
    flat = cls("Led a team of five. Shipped it.")
    spaced = cls("Led  a team\tof five.\r\nShipped   it.\n")
    assert flat.fingerprint == spaced.fingerprint


def test_flat_and_markdown_with_the_same_words_and_breaks_agree() -> None:
    flat = CvForScoring("Skills\nPython\nGo")
    same_breaks = CvForScoring("Skills\r\nPython \r\nGo\r\n")
    assert flat.fingerprint == same_breaks.fingerprint
    assert flat.value == same_breaks.value


def test_different_words_have_different_fingerprints() -> None:
    assert CvForScoring("Python").fingerprint != CvForScoring("Go").fingerprint


def test_markdown_markers_stay_so_they_change_the_fingerprint() -> None:
    assert CvForScoring("# Skills").fingerprint != CvForScoring("Skills").fingerprint


def test_cv_fingerprint_is_sha256_of_the_normalized_text() -> None:
    cv = CvForScoring("Led  a team")
    expected = hashlib.sha256(b"Led a team").hexdigest()
    assert cv.fingerprint == CvFingerprint(expected)


def test_posting_fingerprint_is_sha256_of_the_normalized_text() -> None:
    posting = PostingForScoring("We  need Go")
    expected = hashlib.sha256(b"We need Go").hexdigest()
    assert posting.fingerprint == PostingFingerprint(expected)


def test_character_count_counts_the_normalized_text() -> None:
    assert CvForScoring("ab   cd").character_count == 5
    assert PostingForScoring(" a  b ").character_count == 3


@pytest.mark.parametrize("cls", [CvForScoring, PostingForScoring])
@pytest.mark.parametrize("blank", ["", "   ", "\n\t \r\n"])
def test_blank_text_is_refused(cls: type, blank: str) -> None:
    with pytest.raises(InvalidScoringText):
        cls(blank)


@pytest.mark.parametrize("cls", [CvFingerprint, PostingFingerprint])
@pytest.mark.parametrize(
    "bad",
    ["", "abc", "a" * 63, "a" * 65, "A" * 64, "g" * 64, ("a" * 63) + " "],
)
def test_a_fingerprint_must_be_64_lowercase_hex(cls: type, bad: str) -> None:
    with pytest.raises(InvalidFingerprint):
        cls(bad)


@pytest.mark.parametrize("cls", [CvFingerprint, PostingFingerprint])
def test_a_valid_fingerprint_is_accepted(cls: type) -> None:
    assert cls("0123456789abcdef" * 4).value == "0123456789abcdef" * 4


def test_repr_of_the_texts_shows_no_text() -> None:
    assert SECRET not in repr(CvForScoring(SECRET))
    assert SECRET not in repr(PostingForScoring(SECRET))


def test_repr_of_a_fingerprint_shows_no_hash() -> None:
    fp = CvForScoring(SECRET).fingerprint
    assert fp.value not in repr(fp)
    pfp = PostingForScoring(SECRET).fingerprint
    assert pfp.value not in repr(pfp)


def test_repr_of_a_gap_and_an_assessment_shows_no_text() -> None:
    g = FitGap("Needs Kubernetes at scale")
    assert "Kubernetes" not in repr(g)
    assessment = FitAssessment(ScorePercent(50), (g,))
    assert "Kubernetes" not in repr(assessment)


# --- ScorePercent -------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 50, 99, 100])
def test_score_percent_accepts_zero_to_one_hundred(n: int) -> None:
    assert ScorePercent(n).value == n


@pytest.mark.parametrize("bad", [-1, 101, 1000, True, False, 55.5, "50", None])
def test_score_percent_refuses_out_of_range_bool_and_non_int(bad: object) -> None:
    with pytest.raises(InvalidScorePercent):
        ScorePercent(bad)  # type: ignore[arg-type]


# --- FitGap (AC-25) -----------------------------------------------------------------------------


def test_a_gap_is_normalized() -> None:
    assert FitGap("  Needs   Kubernetes  ").value == "Needs Kubernetes"


def test_a_gap_may_be_3_to_120_characters() -> None:
    assert FitGap("a b").value == "a b"
    assert FitGap("x" * 120).value == "x" * 120


@pytest.mark.parametrize("bad", ["", "  ", "ab", " a ", "x" * 121])
def test_a_gap_outside_3_to_120_characters_is_refused(bad: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(bad)


def test_a_gap_is_measured_after_normalizing() -> None:
    with pytest.raises(InvalidFitGap):
        FitGap("  ab  ")
    assert FitGap("x" * 120 + "   ").value == "x" * 120


@pytest.mark.parametrize("bad", ["line one\nline two", "line one\rline two"])
def test_a_multiline_gap_is_refused(bad: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(bad)


@pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\x7f", "\u0085"])
def test_a_gap_with_a_control_character_is_refused(ch: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(f"needs{ch}python")


@pytest.mark.parametrize(
    "bad",
    [
        "see https://evil.example/x",
        "see http://evil.example",
        "visit www.evil.example now",
        "mail jane@example.com please",
        "call 0123456 now",
        "id 12345678901",
        "phone +1 (555) 1234567",
        "use <b>bold</b> skills",
        "x > y skills",
        "a < b skills",
    ],
)
def test_a_gap_with_url_email_digits_or_markup_is_refused(bad: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(bad)


def test_six_digits_are_allowed_but_seven_are_not() -> None:
    assert FitGap("5 years or 123456 users").value == "5 years or 123456 users"
    with pytest.raises(InvalidFitGap):
        FitGap("handled 1234567 users")


def test_a_gap_error_message_never_quotes_the_value() -> None:
    with pytest.raises(InvalidFitGap) as caught:
        FitGap("mail jane@example.com please")
    assert "jane@example.com" not in str(caught.value)


# --- FitAssessment (AC-27) ----------------------------------------------------------------------


def test_zero_gaps_at_90_or_more_is_valid() -> None:
    assert FitAssessment(ScorePercent(90), ()).gaps == ()
    assert FitAssessment(ScorePercent(100), ()).gaps == ()


@pytest.mark.parametrize("n", [0, 50, 89])
def test_zero_gaps_below_90_is_refused(n: int) -> None:
    with pytest.raises(InvalidFitAssessment):
        FitAssessment(ScorePercent(n), ())


@pytest.mark.parametrize("count", [1, 2, 3])
def test_one_to_three_gaps_are_valid_at_any_score(count: int) -> None:
    gaps = tuple(gap(f"Missing skill number {i}") for i in range(count))
    for n in (0, 89, 90, 100):
        assert FitAssessment(ScorePercent(n), gaps).gaps == gaps


def test_more_than_three_gaps_is_refused_not_truncated() -> None:
    gaps = tuple(gap(f"Missing skill number {i}") for i in range(4))
    with pytest.raises(InvalidFitAssessment):
        FitAssessment(ScorePercent(40), gaps)


def test_duplicate_gaps_are_refused_when_case_folded() -> None:
    with pytest.raises(InvalidFitAssessment):
        FitAssessment(
            ScorePercent(40), (gap("Kubernetes experience"), gap("KUBERNETES Experience"))
        )


# --- MicroUsd -----------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 10**12])
def test_micro_usd_accepts_zero_or_more(n: int) -> None:
    assert MicroUsd(n).value == n


@pytest.mark.parametrize("bad", [-1, 1.5, "1", True, None])
def test_micro_usd_refuses_negative_and_non_integers(bad: object) -> None:
    with pytest.raises(InvalidMicroUsd):
        MicroUsd(bad)  # type: ignore[arg-type]


def test_micro_usd_adds() -> None:
    assert MicroUsd(3) + MicroUsd(4) == MicroUsd(7)


def test_micro_usd_multiplies_by_attempts() -> None:
    assert MicroUsd(250) * 2 == MicroUsd(500)
    assert MicroUsd(250) * 0 == MicroUsd(0)


# --- ScoringPrices ------------------------------------------------------------------------------


@pytest.mark.parametrize("pair", [(0, 1), (1, 0), (0, 0)])
def test_prices_must_each_be_above_zero(pair: tuple[int, int]) -> None:
    with pytest.raises(InvalidScoringPrices):
        ScoringPrices(MicroUsd(pair[0]), MicroUsd(pair[1]))


def test_ten_cents_per_million_prices_1700_tokens_at_170_micro_usd() -> None:
    cost = prices(inp=100_000, out=1).cost_of(usage(input_tokens=1_700), MicroUsd(0))
    # 1,700 input tokens = 170; one output-free call still owes nothing for 0 output tokens.
    assert cost == MicroUsd(170)


def test_each_direction_rounds_up_on_its_own() -> None:
    # 1 input token at 100_000/M = 0.1 -> 1; 1 output token at 400_000/M = 0.4 -> 1. Sum 2, not ceil(0.5)=1.
    assert prices().cost_of(usage(input_tokens=1, output_tokens=1), MicroUsd(0)) == MicroUsd(2)


def test_zero_usage_costs_nothing() -> None:
    assert prices().cost_of(usage(), MicroUsd(999)) == MicroUsd(0)


def test_exact_multiples_do_not_round_up() -> None:
    cost = prices(inp=1_000_000, out=2_000_000).cost_of(
        usage(input_tokens=3, output_tokens=5), MicroUsd(0)
    )
    assert cost == MicroUsd(3 + 10)


def test_each_unmetered_attempt_is_charged_at_the_worst_case() -> None:
    cost = prices().cost_of(usage(input_tokens=1_000_000, unmetered=2), MicroUsd(777))
    assert cost == MicroUsd(100_000 + 2 * 777)


def test_unknown_is_not_zero_even_with_no_metered_tokens() -> None:
    assert prices().cost_of(usage(unmetered=1), MicroUsd(500)) == MicroUsd(500)


def test_worst_case_attempt_rounds_each_direction_up() -> None:
    # input tokens <= chars: (1000 + 500 + 200) = 1700 -> 170 at 100_000/M; output 4000 at 400_000/M -> 1600.
    worst = prices().worst_case_attempt(
        cv_chars=1_000, posting_chars=500, overhead=200, max_output_tokens=4_000
    )
    assert worst == MicroUsd(170 + 1_600)


def test_worst_case_attempt_rounds_up_a_fraction() -> None:
    worst = prices().worst_case_attempt(
        cv_chars=1, posting_chars=1, overhead=0, max_output_tokens=1
    )
    # 2 input tokens -> 0.2 -> 1; 1 output token -> 0.4 -> 1.
    assert worst == MicroUsd(2)
