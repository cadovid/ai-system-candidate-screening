# Responsible AI and human oversight

This document describes engineering controls in the repository and the gaps that remain before any real recruitment use. It is not a privacy notice, a data-protection impact assessment, employment advice, or a legal opinion. The role, service areas, FAQ answers, and criteria shipped here are fictional demo fixtures.

## Intended use and non-goals

The assistant collects a small set of explicit, job-related facts for a delivery-driver screening conversation. It is designed to be transparent about automation, bilingual, interruptible, and reviewable. It does not rank applicants, infer personality or protected characteristics, assess sentiment, search the web, make a subjective hiring recommendation, or send a hire/reject decision to an applicant. `qualified` is only the deterministic result that the configured demo prerequisites were explicitly supplied and confirmed; a recruiter must decide what happens next.

The model is an interpreter, not the decision-maker. Its output schema has intent, language, candidate questions, and typed fact patches with evidence/confidence. It has no status field. A model response is never accepted as proof of licence, location, eligibility, consent, or a protected attribute.

## Control map

| Risk | Implemented control | Observable outcome | Known limitation / next gate |
| --- | --- | --- | --- |
| Candidate mistakes a bot for a person | Introduction and UI disclosure say it is automated AI; the purpose and recruiter handoff are stated | Candidate sees the disclosure before answering | Localise and review the notice for each channel and jurisdiction; test accessibility and comprehension. |
| Prompt injection changes the workflow | Pre-model pattern checks; blocked messages are not passed to the provider; canonical screening facts are not changed | Safe task-focused reply plus `guardrail_blocked` audit event | Regexes are not complete. Add adversarial testing, provider isolation, monitoring, and incident response. |
| Candidate shares sensitive data | Email, phone, card-shaped numbers, and credential-like terms are minimally redacted before persistence/provider work | Redacted message; safety response; no model call | Detection is heuristic and may miss or over-redact. Add a tested DLP service and a deletion/rights workflow. |
| LLM invents an area or criterion | Strict typed output; deterministic catalogue matcher and `ScreeningEngine` own decisions | Exact/alias match, confirmed suggestion, ambiguity, or unsupported result | The fictional catalogue and rules need business-owner review and versioned change control. |
| A correction silently changes a critical fact | Reconciliation stores a `pending_confirmation` proposal and retains the trusted value | Candidate must answer yes/no before the proposal replaces it | Add a complete correction UI, multilingual usability testing, and reviewer visibility for unresolved proposals. |
| Ambiguous data becomes a rejection | Uncertain locations remain active until clarification is exhausted; exhausted attempts produce `needs_review` | Reason code and rule trace identify why a human is needed | Validate outcome parity across languages, dialects, disability-related communication patterns, and channels. |
| Automated result is treated as a hiring decision | Candidate copy says a recruiter reviews; internal view exposes factual state, reason codes, summary, and review endpoint | `handoff_status` can become `reviewed`; review is separately recorded | Integrate authenticated human review, escalation SLAs, contest/appeal handling, and audit sampling before use. |
| Provider outage loses a candidate answer | Provider calls happen after turn reservation; failures save a failed turn and preserve the prior canonical state | `503`, `retryable`, stable error code, and idempotent replay | Add queue/backoff/circuit breaker and a user-visible incident status for production. |
| Duplicate or concurrent requests corrupt state | Unique idempotency constraint, request hash, per-conversation lock, optimistic version check | Safe replay or explicit `409`, never an implicit merge | Process-local locks do not coordinate instances; use a shared store/DB strategy when scaling. |
| Summary leaks or fabricates information | Summary receives validated state + decision only; typed/length checks and `summary_is_safe`; deterministic fallback | `summary_status` is `generated` or `fallback` | Add red-team tests, human summary review, and output retention/access controls. |
| Operational data is overexposed | Candidate and internal tokens are separate; candidate token is hashed; aggregate analytics omit candidate-level data | Protected routes, correlation IDs, operational counters | Add SSO/RBAC, tenant separation, key rotation, secret manager, encryption/backup controls, and access logs. |

## Data minimisation and lifecycle

The canonical state contains the seven configured screening fields, language, current field, confirmation state, bounded clarification counts, and ruleset version. Each accepted field can carry short evidence and confidence for auditability. User and assistant messages are persisted for conversation display and bounded history; user text is passed through the same redaction boundary before storage. A request hash binds an idempotency key to the submitted content without storing the raw request solely for that purpose. Model name, usage, latency, turn status, response payload, result, summary, reviews, and audit events support operations and replay.

The default `RETENTION_DAYS=90` documents an intended configuration, but this checkout does not run a deletion/retention worker. It also does not implement export, access, rectification, erasure, objection, or appeal APIs. A production owner must define purpose, retention, legal basis, processor/provider terms, data-subject request handling, backup deletion, and the treatment of audit records before enabling real candidate traffic. “Do not share sensitive information” is useful product guidance, not a substitute for those controls.

## Human review contract

The deterministic engine emits:

- `status` and stable `reason_codes`;
- `missing_fields` and structured validation issues;
- `ruleset_version` (`2026-01` in this demo); and
- a `rule_trace` naming the evaluated rule and relevant booleans.

Recruiters can list and inspect screening sessions, view canonical state and factual summaries, and record one of the validated review labels: `advance`, `reject`, `needs_review`, `qualified`, or `disqualified`, with optional notes. The review changes handoff metadata and writes an audit event; it does not mutate the candidate’s canonical facts or pretend that a model made the review. Production review should add reviewer identity assurance, role-based permissions, separation of duties, queue ownership, time-to-review monitoring, a candidate contact path, and a way to contest or correct facts.

## Fairness and evaluation posture

The supplied deterministic fixtures test language parity at a shallow level: Spanish and English happy paths use the same explicit criteria. They also cover no licence, unsupported/ambiguous areas, opt-out, correction, unsupported FAQ, prompt injection, and provider failure. `run_evals.py --mode deterministic` runs the checked-in cases with no network or provider key. Live evaluation is manual-only and requires the allowlisted model and a caller-entered spend approval threshold; the workflow does not enforce a provider billing cap, so a provider/project quota is required.

These tests do not establish fairness, validity, accessibility, or generalisation. They contain synthetic names, short messages, and a small catalogue. Before deployment, create a versioned test set representing realistic language variation and legitimate accommodations; measure extraction error, clarification burden, false disqualification, review rates, latency, and outcome parity by permitted audit dimensions. Keep those audit dimensions out of eligibility state, apply access controls, document sampling, and involve domain/legal/privacy reviewers in deciding what may be measured.

## EU orientation (checked 2026-09-02)

The links below are official sources for engineering orientation and should be rechecked at deployment. They do not classify this demo or answer whether a particular deployment is lawful.

As of 2026-09-02, the European Commission’s [AI Act overview](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai) describes general application from 2 August 2026, with prohibitions and AI literacy provisions applying from 2 February 2025, governance and general-purpose-AI provisions from 2 August 2025, and the Commission/national enforcement framework operating from 2 August 2026. The Commission’s [enforcement timeline](https://digital-strategy.ec.europa.eu/en/policies/enforcement-ai-act) reports that Annex III high-risk rules for employment use cases apply from 2 December 2027 and high-risk AI embedded in regulated products from 2 August 2028. The current [consolidated Regulation (EU) 2024/1689](https://eur-lex.europa.eu/eli/reg/2024/1689) and its Annex III 4(a) should be read directly because amendments and implementing material can change the timetable and interpretation.

Recruitment and selection appear in Annex III 4(a) of the consolidated text. This repository therefore treats real employment use as a risk-assessment and review trigger, not as a claim that the demo is or is not high-risk. Product counsel and the deployer must classify the concrete purpose, role, inputs, decisions, human review, provider relationship, and Member State context. The design choices here—disclosure, human review, traceable rules, bounded outputs, and no protected-attribute inference—are prudent engineering measures, not a compliance certificate.

For data protection orientation, consult the official [GDPR text on EUR-Lex](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng/), the [EDPB data-subject-rights overview](https://www.edpb.europa.eu/topics/key-gdpr-concepts/data-subject-rights_en), and the EDPB’s [automated decision-making and profiling guidance page](https://www.edpb.europa.eu/documents/guideline/automated-decision-making-and-profiling_en). They cover concepts relevant to this design such as transparency, access, rectification, erasure, objection, and safeguards around solely automated decisions. This document intentionally avoids translating those sources into a legal conclusion.

## Immediate safety checklist

Before exposing a real candidate channel, require written sign-off on the use case and criteria; an accessible, channel-specific disclosure and privacy notice; a provider/data-flow review; identity, secret, role, and tenant controls; encryption and retention/deletion; an incident and red-team plan; deterministic replay and migration tests; multilingual and accessibility tests; human review and contest routes; and monitoring for extraction errors, drift, outcome disparities, and guardrail misses. Keep the demo fixtures clearly labelled until each gate is complete.
