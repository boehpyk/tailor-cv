"""The prompt eval behind `make eval` (`tailorcraft.cli eval-prompts`, task T37). **Not a test.**

It runs the committed corpus (`api/eval/corpus/`) through the **real** `GeminiLlm` pipeline — prompt,
call, re-validation — against the real API, by hand, and it costs money. It is not in `make check`
and must never be: CI has no key, and ADR-0004 is explicit that prompt quality is judgement. What it
does is split the work honestly:

- **Asserted, because it is cheap and objective** (`checks.py`): both documents present, lengths
  inside the value objects' own bounds, no known employer name in a tailored CV that the base CV does
  not contain, every employer and education entry the base CV declares still named in the tailored
  CV, the candidate's declared name present in both documents, and thinking actually off.
- **Measured** (`results.py`): `llm_duration_ms`, token counts and outcome per run; p50/p95 and mean
  tokens across runs, for AC-20(b); a loud verdict against Constitution §7's 15-second budget.
- **Printed for a human** (`report.py`): each tailored document side by side with the text it came
  from, plus what to look for in that pair. Exit 0 means the cheap checks passed. It does not mean
  the documents are good.

**How `thoughts_token_count` is captured without touching the domain.** The owner disabled thinking
(`ThinkingConfig(thinking_budget=0)` in `build_generate_config`), and the SDK calls that setting
"model dependent", so only a real call can show `gemini-2.5-flash` honours it. `LlmCallMetrics` does
not carry thoughts and should not: the domain has no concept of a model thinking. Instead
`RawCompletion` — an infrastructure type that never crosses the port — carries the provider's count
verbatim, and `GeminiLlm.generate` exposes the adapter's real SDK path read-only. The runner wraps that
path in `RecordingGenerate` and injects the wrapper into a second `GeminiLlm`, so every step of
`tailor()` still runs (timeouts, retries, parsing, metrics) while each individual SDK call's usage is
recorded on the side. Nothing here imports the SDK; `gemini.py` remains the only file that does.
"""
