# Candidate screening process design

## Purpose and boundaries

The assistant is a disclosed automated helper for a fictional delivery-driver role. It collects only the approved job-related facts: full name, valid driver’s licence, city/zone, availability, preferred schedule, delivery experience, and possible start date or period. It does not rank candidates, infer protected characteristics, assess personality or sentiment, invent a service area, or make a subjective employment decision. A recruiter reviews terminal and uncertain cases.

The conversation language (`es` or `en`) controls wording, not the criteria. The canonical state is language-independent and is evaluated by ruleset `2026-01`. The disclosure is product copy and `disclosure_acknowledged` is a state signal; neither is presented as legal consent or an eligibility signal.

## Stage 1 — start and disclosure

`POST /api/v1/candidate/conversations` creates a candidate, screening session, and active conversation. The request may select Spanish or English, a channel, and an optional display name. The response returns IDs, the first assistant message, state version `1`, and an opaque resume token. Only the token hash is stored; the token is needed for later candidate reads and turns.

The introduction identifies the assistant as automated AI, states that it collects job-related details for a recruiter, says the candidate can stop at any time, warns against unrelated sensitive data, and asks whether the candidate wants to continue. The UI language selector is the primary language control. An explicit language request in a later message can switch the active language; high-confidence detected language may also change wording. No language signal changes a rule or status.

## Stage 2 — guardrails and turn reservation

For each candidate turn, the API requires a bearer resume token and an idempotency key. Middleware applies the request-body limit, process-local rate limit, and non-blocking turn concurrency limit. The coordinator then enforces a non-empty message and the configured 2,000-character input bound.

Before any model call, `inspect_message` checks for common prompt-injection patterns and sensitive/contact-like content. It minimally redacts email addresses, phone numbers, card-shaped digit strings, and credential terms such as password, API key, IBAN, NIF, NIE, CURP, or RFC. The redacted text is the message retained in the conversation and, when allowed, the text sent to the interpreter. A blocked message does not reach the provider and cannot mutate canonical screening facts. The original message is used only to derive a one-way request fingerprint for idempotency.

The coordinator reserves the turn in a short transaction, stores the redacted user message, records `turn_started`, and commits before waiting on the provider. This prevents provider latency from holding a database transaction open.

## Stage 3 — interpretation, not decision

The Pydantic AI interpreter is the normal semantic path. It receives trusted server dependencies: the current canonical state, current field, active language, current date, bounded history, and correlation ID. History is built by the server from persisted messages, capped at six user/assistant pairs and 8,000 characters by default; clients cannot submit model messages.

The output is a strict `TurnInterpretation` containing intent, language signal, response-needed flag, opt-out/safety flags, candidate questions, and typed patches for screening fields. Each patch may include evidence from the latest message, confidence, ambiguity, and correction metadata. The schema has no status or qualification field. Groq retries a transient 408/409/429/5xx response up to two times after the initial request, honoring `Retry-After` or applying jittered exponential backoff. Only after that budget is exhausted does the provider error become a failed turn with a safe message naming the pending field; the previous canonical state remains unchanged.

Every normal safe text turn calls the selected provider first. If the structured response validates but is neutral or empty—no usable field patch, confirmation, language signal, candidate question, or relevant intent—the coordinator may invoke the application-owned deterministic interpreter for a narrow, auditable completion: exact controls, exact catalogue locations, explicit name prefixes, or short title-cased bare names. Any completion is merged without replacing usable model facts and its provenance is retained. Ambiguous values, corrections, candidate questions, off-topic turns, and model-reported safety signals remain model-owned; deterministic reconciliation, catalogue matching, response planning, and qualification remain authoritative. Guardrail blocks, exact opt-outs, explicit UI language events, and `MAX_TURNS` are provider-free control exceptions. Provider/API, timeout, rate-limit, missing-credential, and invalid-schema failures are distinct from an insufficient response: they become safe retryable failures and do not invoke semantic deterministic recovery or switch providers/models. After every valid model response, location recovery grounds every non-empty patch against the current candidate message and any trusted pending-city context; a stale location echoed from bounded history is discarded before reconciliation, even when the provider omitted `provided=true`. Only catalogue-grounded text can recover an area, and an unsupported model guess never becomes a trusted location.

The deterministic evaluator in `run_evals.py` exercises the provider-independent rules, guardrails, control exceptions, and narrow completion behavior without making a provider request. Its deliberately small fixture parser covers scripted multi-field, correction, FAQ, code-switch, and ambiguity phrases so the 24 evaluator scenarios remain network-free and reproducible; it is regression coverage for state/rule behavior, not an accuracy claim about the language model. Live runs exercise the provider-first path and should report interpretation provenance separately from deterministic completion or provider failure.

## Stage 4 — reconcile explicit facts

`reconcile_interpretation` copies the current state and applies only explicit, valid patches. It stores bounded evidence and confidence with accepted fields, but a changed value never silently replaces an existing value. A correction or contradiction creates `pending_confirmation`; a positive confirmation applies it, while a negative confirmation discards it. During a pending correction, another correction is not stacked. A model-originated unsupported location is a decision-impacting claim, so it is held with `reason: "decision_impact_confirmation"` until the candidate confirms it; an application-owned catalogue result can be evaluated directly. Final candidate confirmation is reset whenever a factual value changes.

Fields are requested in this order, skipping values already present:

1. full name;
2. valid driver’s licence (yes/no);
3. city and service area;
4. availability (full time, part time, weekends);
5. preferred schedule (morning, afternoon, evening, flexible);
6. delivery experience in years, with optional platforms; and
7. start availability, preserving a natural-language value and a safe precision (`exact`, `asap`, `week`, `month`, or `unknown`).

An actionable relative period such as “next week”, “next month”, or “as soon as possible” is sufficient even when the candidate does not provide an exact calendar date. The language interpreter should set the corresponding typed precision and leave the date empty; only genuinely unclear timing (for example, “sometime”) is ambiguous. This guidance affects extraction only—the deterministic rules evaluate the validated canonical value.

A single natural-language answer may provide several fields. The assistant asks one clear next question after applying the supported fields, and does not ask the model to compose an eligibility explanation. The controller instead builds a deterministic `ResponsePlan` and renders bounded, localized copy; live turns choose from a small natural variant set while tests inject the selector and persisted messages make retries/recovery stable. Copy variation never changes state or decisions.

When a semantic interpretation contains no usable patch, the controller does not silently replay the normal question. It creates a field-specific clarification. If the typed interpretation safely contains an uncertain but useful value, the clarification names what was heard—for example, “I heard a flexible schedule”—and asks only for the missing confirmation. After the bounded clarification limit, the recruiter handoff names the unresolved field. A candidate question or genuine off-topic detour is treated separately and bridges back to the pending screening goal.

## Stage 5 — resolve location and questions

Locations are matched by the deterministic, data-driven catalogue. Normalisation removes case/accents, punctuation, and excess whitespace. A unique exact name or configured alias is accepted, including common Spanish/English forms such as `Madrid centro`, `Madrid center`, and `The city center of Madrid`. A model may provide city/zone hints, but Python remains authoritative: a shortened zone phrase such as `city center` is accepted with a known `city=Madrid` only when it is compatible with a configured Madrid alias, and contradictory raw text cannot be overridden by a model hint. A single fuzzy suggestion is held for explicit candidate confirmation. Several matches remain `ambiguous`; no suggestion is selected. A known city without a safely identified zone is a deliberate city-level offer: the assistant lists every configured zone for that city and asks whether the candidate can deliver in any of them. A positive answer stores the explicitly confirmed set of configured area IDs; a negative answer is deterministically outside the service area. A value with no recognized city or acceptable suggestion is `unsupported`; when that conclusion comes from the semantic model rather than an application-owned exact result, it first requires the decision-impact confirmation described above. The matcher returns IDs and evidence that can be audited, rather than allowing a model to invent coverage.

Candidate questions are handled without widening the knowledge boundary. `FAQCatalog` performs accent-insensitive token/phrase matching across the bilingual fictional FAQ and returns the configured answer in the active language, then resumes the screening prompt. Unsupported questions receive an honest bounded response that a recruiter can follow up; there is no embeddings, vector database, web search, or open-ended RAG answer in this demo. Off-topic requests do not add facts.

## Stage 6 — deterministic evaluation

`ScreeningEngine` evaluates the reconciled `ScreeningState` in stable order:

- An explicit `drivers_license=false` yields `disqualified` with `no_drivers_license`.
- An `unsupported` location yields `disqualified` with `outside_service_area`; the response echoes the exact interpreted location. If a known city was supplied, it also names that city's configured zones.
- A known city without a zone remains `in_progress` with `ambiguous_location` and a `service_area_city` confirmation. A positive answer accepts the configured city-area set; a negative answer yields `disqualified` with `outside_service_area`.
- Other ambiguous locations remain `in_progress` with `ambiguous_location` while clarification is available; a single fuzzy suggestion is held as `pending_confirmation` and remains `in_progress` until the candidate confirms or rejects it.
- A pending correction or any other missing field yields `in_progress` with an explanatory reason.
- After two configured clarification attempts for the same issue, the controller yields `needs_review` with `retry_limit` and retains trusted values. FAQ, off-topic, language-only, and explicit confirmation turns are not counted as failed answers for that bound.
- When all required fields are present but final confirmation is not true, the result is `in_progress` with `awaiting_candidate_confirmation`.
- A complete, explicit, confirmed state is deterministically eligible for `qualified` with `all_explicit_criteria_met`. The conversation controller temporarily presents it as `in_progress` with `awaiting_candidate_questions` until the candidate explicitly finishes the post-screening FAQ phase; this conversation gate is not an eligibility criterion.

The decision includes missing fields, validation issues, ruleset version, and a rule trace. It is independent of language, provider wording, summary wording, or recruiter preference. An ambiguous location remains `in_progress` while the controller asks for clarification; after the configured clarification bound, it becomes `needs_review`. A single fuzzy suggestion remains `in_progress` until the candidate explicitly confirms or rejects it. The domain transition table permits `in_progress`/`needs_review` to continue or become terminal, while `qualified`, `disqualified`, and `abandoned` are sticky. In the HTTP workflow, `needs_review` is handed to a recruiter and the candidate conversation is closed.

## Stage 7 — post-screening questions, commit, handoff, and summary

After the candidate confirms the canonical review and the rule engine returns `qualified`, the controller asks whether they have questions about the company, role, or hiring process. A question is answered through the same bounded fictional FAQ catalogue and followed by an invitation to ask another. Saying that there are no questions, or no more questions, sets the persisted conversation-control flag and allows the already-determined qualified result to become terminal. Saying yes merely opens the question phase; it does not finish it. Corrections during this phase reopen final review and re-run deterministic validation. The FAQ flag never changes licence, location, or any other hiring criterion.

The coordinator conditionally saves the new state with `screening_sessions.version`, records the assistant message and response payload, and appends `turn_completed` plus any terminal event. Terminal screening states close the conversation (`completed`); opt-out closes it as `opted_out`. The candidate receives the criterion-specific close or recruiter-review message. For non-terminal turns, the controller chooses a `ResponsePlan` (`prompt`, `clarification`, `pending_confirmation`, FAQ bridge, post-screening FAQ offer/follow-up, unknown-question bridge, or final confirmation) and the deterministic renderer supplies the localized copy. Final confirmation lists every canonical screening value before asking whether it is correct. Provider wording can change the extracted patch, but it does not directly become a screening decision or unbounded response text. Turn metadata preserves the selected model/provider attempt and any provider-reported usage, then labels whether the LLM result was used, a valid neutral/empty result required deterministic completion, or a provider-free control handled the turn; failure categories remain separate.

For `qualified`, `disqualified`, or `needs_review`, a summary is generated after the state transaction. The optional summary agent receives validated state and the deterministic decision, not the transcript. Output is schema-validated, capped at 1,200 characters, and checked for prompt leaks, PII, and protected-attribute-like content. Unsafe or failed output becomes a factual deterministic fallback; `summary_status` records `generated` or `fallback`. Qualified and review outcomes use `handoff_status=ready`; a recruiter review changes it to `reviewed` and adds an audit event. `MAX_TURNS` is enforced by the coordinator: the final unresolved turn is handed off deterministically, without another provider call, and the conversation is closed.

## Edge-case contract

`STOP`, `unsubscribe`, `cancel`, `baja`, `salir`, and similar opt-out signals produce `abandoned` and suppress re-engagement. A closed conversation rejects later turns. Inactive active conversations remain `in_progress`; `scripts/reengage.py` is dry-run by default and may queue at most the configured number of reminders. It never automatically marks a candidate abandoned, and opted-out/terminal conversations are suppressed.

Duplicate requests with the same conversation/key/message replay the stored response with `idempotent=true`. Reusing a key for another message is a conflict. Concurrent updates fail safely on an optimistic version conflict rather than merging uncertain model patches. Provider, persistence, or unexpected HTTP failures return bounded messages and correlation IDs; no stack trace or provider secret is shown. Candidate and internal credentials are separate. These controls are process-local where noted, so a multi-instance deployment needs shared coordination.

## Conversation style

Responses are short, respectful, and action-oriented. The assistant says what it can and cannot answer, asks one focused question, never claims a job outcome, and keeps the candidate’s language choice. Recruiter-facing summaries are factual and criterion-based. All demo copy and data must be replaced and reviewed before real use.
