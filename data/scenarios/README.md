# Conversation scenarios

`scenarios.json` is a reviewable fixture set for deterministic smoke tests and
evaluator runs. Each item has an `id`, ordered candidate `turns`, and an
`expected` object. The 24 fixtures deliberately exercise first-turn
multi-field answers, real corrections, language switches, code-switching, FAQ
and off-topic interruptions, prompt injection, duplicate/retry behavior, and
provider/malformed output failures.

The natural-language cases use supplied conversational phrasing rather than
only labelled `name: ...; location: ...` strings. Deterministic evals use a
bounded provider-independent fixture parser and the canonical location/rules
helpers to keep state regression checks network-free; this describes the eval
harness, not the runtime ordering. In the live runtime, every normal safe turn
tries the selected LLM first, with narrow deterministic completion only for a
valid neutral/empty structured response. The fixture parser is not a general
natural-language engine.

The expected status is always checked against the deterministic screening
engine; it is not an LLM-provided decision. Expectations may additionally
specify reason codes, canonical state fragments, language traces, FAQ answers,
correction confirmation, security events, retryability, and error types.

Qualified fixtures include the final post-screening FAQ response: after the
candidate approves the canonical review, a final `No` means there are no more
questions and allows the conversation to close. This is conversation control,
not an additional eligibility rule.

The correction fixture deliberately uses `next week`: the interpreter should
preserve it as an unambiguous `week` start period without inventing a calendar
date. Vague periods remain eligible for clarification instead.
