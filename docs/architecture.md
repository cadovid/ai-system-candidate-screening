# Architecture

This service is a deliberately small modular monolith. The modules have clear seams so a provider, database, or HTTP adapter can be replaced without moving eligibility logic into a prompt. The runtime entry point is [`src/candidate_screening/main.py`](../src/candidate_screening/main.py); the domain package has no LLM or SQLAlchemy dependency.

## Components

```mermaid
flowchart TB
  subgraph Clients
    TextUI[Candidate Text UI: textarea + Send]
    VoiceControls[Candidate Voice controls]
    BrowserSTT[Browser SpeechRecognition / STT]
    BrowserTTS[Browser speechSynthesis / optional TTS]
    Recruiter[Recruiter UI / internal client]
    VoiceControls -->|spoken input| BrowserSTT
    BrowserSTT -->|interim/final transcript| TextUI
    TextUI -->|typed or edited text + input_mode| Turns["POST /api/v1/candidate/conversations/{id}/turns"]
    TextUI -->|new conversation| Create["POST /api/v1/candidate/conversations"]
    Turns -->|assistant text| TextUI
    Turns -->|optional assistant text| BrowserTTS
  end

  subgraph HTTP[FastAPI application]
    Middleware[Correlation ID + body/rate/concurrency limits]
    CandidateAPI[Candidate API]
    InternalAPI[Internal/recruiter + analytics API]
    Auth[Opaque resume token / internal bearer key]
  end

  subgraph Application[Application layer]
    Coordinator[TurnCoordinator]
    Controller[ConversationController]
    Reconcile[reconcile_interpretation]
    Guard[inspect_message + summary_is_safe]
    FAQ[FAQCatalog]
    Analytics[operational event aggregation]
    Reminder[ReengagementService]
  end

  subgraph Domain[Pure domain layer]
    State[ScreeningState]
    Rules[ScreeningEngine]
    Areas[ServiceAreaMatcher]
    Transitions[allowed status transitions]
  end

  subgraph AI[Separate server-side LLM provider boundary]
    Resolver[Configuration-driven model resolver]
    Interpreter[PydanticAIInterpreter]
    Summary[SummaryGenerator]
    Schemas[TurnInterpretation / RecruiterSummaryOutput]
    OpenAI[OpenAI Responses model]
    OpenRouter[OpenRouter / selected model]
  end

  OpenAI -. server-side API .-> OpenAIAPI[(OpenAI API)]
  OpenRouter -. server-side API .-> OpenRouterAPI[(OpenRouter API)]

  DB[(SQLAlchemy ORM + SQLite)]
  Data[(versioned JSON catalogue + FAQ)]

  Turns --> Middleware
  Create --> Middleware
  Recruiter --> Middleware
  Middleware --> CandidateAPI
  Middleware --> InternalAPI
  CandidateAPI --> Auth
  InternalAPI --> Auth
  CandidateAPI --> Coordinator
  InternalAPI --> Coordinator
  Coordinator --> Guard
  Guard --> Interpreter
  Resolver --> OpenAI
  Resolver --> OpenRouter
  OpenAI --> Interpreter
  OpenRouter --> Interpreter
  OpenAI --> Summary
  OpenRouter --> Summary
  Interpreter --> Schemas
  Schemas --> Reconcile
  Reconcile --> Controller
  Controller --> Rules
  Controller --> Areas
  Coordinator --> FAQ
  FAQ --> Data
  Coordinator --> Summary
  Summary --> Guard
  Coordinator --> DB
  InternalAPI --> Analytics
  Reminder --> DB
  State --> Rules
  Transitions --> Rules
```

The candidate browser has one text composer and two optional voice adapters. Typed text, or a reviewed transcript produced by browser SpeechRecognition, is submitted to the same versioned `/turns` endpoint and enters the same coordinator/reconciliation/rules workflow; `input_mode` is provenance, not a different decision path. Assistant text is always rendered in the text UI, with optional browser `speechSynthesis` reading it aloud. Microphone audio is not sent to or persisted by this service. The separate server-side provider boundary is used only after guardrails, and both supported providers feed the same typed interpreter contract.

### HTTP and security boundary

FastAPI exposes a versioned candidate router at `/api/v1/candidate`, a protected internal router at `/api/v1/internal`, a protected analytics endpoint at `/api/v1/internal/analytics`, and public `/healthz`, `/livez`, and `/readyz` endpoints. Unversioned aliases exist for local compatibility and are intentionally omitted from OpenAPI. Middleware adds a bounded correlation ID, rejects bodies above `MAX_BODY_BYTES`, applies a process-local rolling request limit to candidate paths, and rejects new turns when the process-local semaphore is full.

Conversation creation has no bearer credential because it creates a new session. The response contains an opaque high-entropy resume token once; the database stores only its SHA-256 hash. Candidate reads and turns require `Authorization: Bearer <resume token>`. Internal routes require a separately configured `INTERNAL_API_KEY` and compare it in constant time. A candidate token cannot be used as an internal credential.

### Application and domain boundary

`TurnCoordinator` is the transaction boundary and failure policy. It reserves a turn, persists the redacted candidate message, builds server-owned bounded history, calls the interpreter outside a database transaction, invokes `ConversationController`, and commits the resulting state with an expected version. It then records a response payload that allows an idempotent replay. The coordinator never trusts a model-provided status.

`ConversationController` delegates factual mutation to reconciliation and status evaluation to `ScreeningEngine`. Reconciliation accepts only fields explicitly supported by `TurnInterpretation`. It stores evidence with a message ID and confidence, but an existing value is not overwritten by a changed value: the proposed value is stored in `pending_confirmation` until a positive confirmation. Prompt injection and sensitive-data flags return a safe response without applying any patch.

The rule engine is pure and rerunnable. Under ruleset `2026-01`, the required fields are full name, licence, confirmed exact service area, availability, preferred schedule, delivery experience, and start availability, followed by candidate confirmation. The only disqualification rules are an explicit negative licence answer and an unsupported service area. Missing data and uncertain locations are `in_progress`; the controller changes an ambiguous location to `needs_review` only after its clarification bound is exhausted. A single fuzzy suggestion remains pending and answerable until explicit confirmation or rejection. An explicit opt-out is `abandoned`; a complete, confirmed state is `qualified`. `qualified`, `disqualified`, and `abandoned` are sticky through the transition table. The coordinator also closes the candidate conversation when it hands off `needs_review`, even though the domain transition table permits later internal resolution.

### AI boundary

`PydanticAIInterpreter` uses a Pydantic AI `Agent` with `TurnInterpretation` as its output type. The schema is an extraction/intent patch, not a decision schema, and rejects unknown fields. Trusted dependencies include current canonical state, pending field, active language, date, and correlation ID. Persisted history is converted to provider messages by `build_bounded_history` and limited by pair and character budgets.

The model factory is the only provider-specific construction boundary. `LLM_MODEL` uses `provider:model` syntax and currently resolves either `openai:<model>` through `OpenAIResponsesModel` or `openrouter:<model>` through Pydantic AI's first-class `OpenRouterModel`/`OpenRouterProvider` ([official integration](https://pydantic.dev/docs/ai/models/openrouter/)). Both providers feed the same typed extraction and summary agents and the same coordinator. OpenRouter is configured with required request parameters, no model fallback, and conservative data-collection/ZDR routing settings; a free-route outage becomes a safe retryable turn failure rather than an unexpected paid request. Provider keys are selected lazily from `OPENAI_API_KEY` or `OPENROUTER_API_KEY`, and are never returned to the browser or written to logs.

The summary agent is a separate typed agent. It receives JSON containing the validated state and deterministic decision, not the raw transcript. A summary is accepted only if it validates, is at most 1,200 characters, and passes the deterministic safety predicate. Any provider error or unsafe output uses `_summary_fallback`, which omits free-form evidence and raw start-date text. Summary generation is intentionally after the state commit; a slow model cannot keep a database transaction open or roll back a valid screening result. The canonical result starts as `summary_status=pending`; a transient derived-write failure is logged without failing the candidate turn, and an authenticated `POST /api/v1/internal/screenings/{session_id}/summary/retry` repairs the summary without re-running screening.

### Knowledge boundary

The FAQ is not an open-ended model knowledge source. `FAQCatalog` loads a small bilingual JSON file, accent-folds and tokenizes the candidate’s question, and returns the highest-scoring configured phrase/keyword match. It answers in the active language and then resumes the next screening prompt. No vector store, embeddings, web search, citations, or semantic retrieval are implemented. The service-area catalogue follows the same data-driven approach: exact names and aliases are accepted, one fuzzy suggestion requires confirmation, and multiple suggestions remain ambiguous.

## Turn sequence and consistency

```mermaid
sequenceDiagram
  participant T as Candidate Text UI
  participant V as Browser Voice (STT/TTS)
  participant A as API/middleware
  participant C as TurnCoordinator
  participant G as Guardrails
  participant M as Interpreter
  participant D as Domain controller
  participant S as ScreeningEngine
  participant DB as Database
  participant H as Summary agent

  alt typed input
    T->>A: POST same /turns endpoint + input_mode text + bearer + idempotency key
  else browser voice input
    V->>V: SpeechRecognition captures speech
    V->>T: Display editable transcript
    T->>A: POST same /turns endpoint + input_mode voice + bearer + idempotency key
  end
  A->>C: validated message
  C->>G: inspect and minimally redact
  C->>DB: reserve turn, persist redacted user message
  DB-->>C: state snapshot + version
  alt blocked by guardrail
    C->>D: typed safety interpretation
  else safe text
    C->>M: redacted text + bounded server history
    M-->>C: TurnInterpretation
    C->>D: reconcile typed patch
  end
  D->>S: canonical ScreeningState
  S-->>D: decision + missing fields + rule trace
  C->>DB: conditional state/version update + response + audit event
  DB-->>C: committed result
  opt terminal result
    C->>H: validated state + decision
    H-->>C: bounded summary or failure
    C->>DB: summary or deterministic fallback
  end
  C-->>A: CandidateTurnResponse
  A-->>T: assistant text + status/version
  opt read-aloud enabled
    T->>V: assistant text
    V->>V: speechSynthesis reads response
  end
```

The unique `(conversation_id, idempotency_key)` constraint and request hash prevent a retry from becoming a second turn. Replaying a completed or failed turn returns the stored response with `idempotent: true`; using the key for a different message is a conflict. The coordinator enforces `MAX_TURNS` before any additional provider call and uses a deterministic recruiter handoff at the bound. An in-process per-conversation lock reduces duplicate work, while optimistic `screening_sessions.version` checking protects the database if another worker changes the session. A conflict is reported as a safe retryable/concurrency error rather than merging model updates.

## Persistence model

The Alembic schema stores `candidates`, `screening_sessions`, `conversations`, `turns`, `messages`, `screening_results`, `audit_events`, and `recruiter_reviews`. Canonical state is JSON so the domain model can be validated and replayed; status, language, version, activity timestamps, and ruleset version have first-class columns for indexing and operations. Turns retain model name/usage and latency for operations, but candidate-facing responses contain neither provider traces nor stack traces. Audit events hold stable event names such as `turn_started`, `turn_completed`, `guardrail_blocked`, `screening_qualified`, `summary_fallback`, and `recruiter_review_recorded`.

The default SQLite URL and one Uvicorn worker are appropriate for a local/demo process. The ORM uses portable primitives and an explicit unit of work, but a multi-instance deployment still needs a managed database, shared rate limiting, a queue/worker strategy, and migration coordination. `/livez` is a static process probe. `/readyz` performs a narrow `SELECT 1` database check plus service-area/FAQ fixture parsing; it deliberately does not require an LLM provider, because provider availability is handled per turn with a safe retryable response.

## Operational jobs

`ReengagementService` is a separate application service invoked by [`scripts/reengage.py`](../scripts/reengage.py). It plans only active, still-in-progress conversations older than `INACTIVITY_HOURS`, suppresses opted-out/closed/terminal sessions and conversations at the reminder limit, and defaults to a dry run. `--apply` conditionally claims each reminder, appends the assistant message and audit event, and is safe to retry. It does not automatically mark a conversation abandoned. The `RETENTION_DAYS` setting is present for a future retention job; no deletion worker currently exists.
