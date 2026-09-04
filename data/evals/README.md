# Evaluation cases

`eval_cases.json` maps evaluation IDs to the deterministic conversation
fixtures in `../scenarios/scenarios.json`. The suite contains 20 cases covering
normal screening, multi-field extraction, corrections and confirmation,
bilingual/code-switched turns, FAQ and off-topic interruptions, security
guardrails, retries, and provider/malformed output failures.

Each case can assert more than a terminal status:

- `reason_codes` checks the deterministic explanation for the outcome;
- `canonical_fields` checks trusted state (including nested location data);
- `language` and `language_trace` check language selection and switches;
- `faq` and `correction` check interruption and confirmation behavior;
- `security_event`, `retryable`, and `error_type` check failure semantics.

The live runner accepts the same shape and only differs in how each turn is
interpreted. It uses the provider selected by `LLM_MODEL` (Groq, OpenRouter, or
OpenAI); the manual live workflow and the opt-in typed provider contract are
documented in the root `README.md`. No candidate data or provider credential
belongs in this directory.

The `correction_requires_confirmation` fixture uses `next week` to exercise
the model extraction contract for an actionable relative start period. Its
expected canonical precision is `week` with no exact date; the deterministic
engine still evaluates only the validated state.
