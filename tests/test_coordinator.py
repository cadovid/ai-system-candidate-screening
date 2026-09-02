from __future__ import annotations

from candidate_screening.application.coordinator import generate_resume_token, hash_resume_token


def test_resume_tokens_are_opaque_and_hashed() -> None:
    token = generate_resume_token()
    assert len(token) >= 40
    assert hash_resume_token(token) == hash_resume_token(token)
    assert hash_resume_token(token) != token
