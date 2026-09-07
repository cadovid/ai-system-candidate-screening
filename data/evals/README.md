# Evaluation cases

`eval_cases.json` maps evaluation IDs to the deterministic conversation
fixtures in `../scenarios/scenarios.json`. The suite contains 24 cases covering
normal screening, multi-field extraction, corrections and confirmation,
bilingual/code-switched turns, FAQ and off-topic interruptions, security
guardrails, retries, and provider/malformed output failures. Deterministic mode
intentionally bypasses the provider so canonical rules, provider-free controls,
and narrow completion behavior remain cheap and reproducible; it is not a
measure of LLM extraction quality.

Each case can assert more than a terminal status:

- `reason_codes` checks the deterministic explanation for the outcome;
- `canonical_fields` checks trusted state (including nested location data);
- `language` and `language_trace` check language selection and switches;
- `faq` and `correction` check interruption and confirmation behavior;
- `security_event`, `retryable`, and `error_type` check failure semantics.

Qualified cases also assert the post-screening FAQ gate: the screening facts
are already deterministically eligible, but terminal completion occurs only
after the candidate explicitly indicates that there are no more questions.

The live runner accepts the same shape but exercises the provider-first typed
interpreter path. It uses the provider selected by `LLM_MODEL` (Groq,
OpenRouter, or OpenAI); a valid neutral/empty structured response may invoke
the narrow deterministic completion path, while ambiguous/correction/question
output remains model-owned and provider failures remain failures. The manual
live workflow and the opt-in typed provider contract are documented in the
root `README.md`. No candidate data or provider credential belongs in this
directory.

Keep live runs small and explicit with repeated `--case-id` arguments. The
24-case deterministic suite is the ordinary regression gate; a live run is an
operator-approved smoke test for model behavior and usage provenance, not a
replacement for the provider-free suite.

The `correction_requires_confirmation` fixture uses `next week` to exercise
the model extraction contract for an actionable relative start period. Its
expected canonical precision is `week` with no exact date; the deterministic
engine still evaluates only the validated state.
