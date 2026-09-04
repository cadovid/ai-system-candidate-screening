# Conversation scenarios

`scenarios.json` is a reviewable fixture set for deterministic smoke tests and
evaluator runs. Each item has an `id`, ordered candidate `turns`, and an
`expected` object. The 20 fixtures deliberately exercise first-turn
multi-field answers, real corrections, language switches, code-switching, FAQ
and off-topic interruptions, prompt injection, duplicate/retry behavior, and
provider/malformed output failures.

The expected status is always checked against the deterministic screening
engine; it is not an LLM-provided decision. Expectations may additionally
specify reason codes, canonical state fragments, language traces, FAQ answers,
correction confirmation, security events, retryability, and error types.

The correction fixture deliberately uses `next week`: the interpreter should
preserve it as an unambiguous `week` start period without inventing a calendar
date. Vague periods remain eligible for clarification instead.
