from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic_ai.models import override_allow_model_requests

from candidate_screening.domain.service_areas import ServiceAreaMatcher


@pytest.fixture
def service_area_matcher() -> ServiceAreaMatcher:
    path = Path(__file__).parents[1] / "data" / "service_areas" / "service_areas.json"
    return ServiceAreaMatcher.from_file(path)


@pytest.fixture(autouse=True)
def prohibit_external_model_requests() -> Iterator[None]:
    """Keep the deterministic suite from making paid/network model calls.

    Pydantic AI exposes this official process-local switch for model
    implementations that call ``check_allow_model_requests``.  TestModel and
    FunctionModel intentionally bypass the switch, so adapter tests continue
    to exercise typed model behavior while an accidentally constructed live
    provider fails immediately.
    """

    with override_allow_model_requests(False):
        yield
