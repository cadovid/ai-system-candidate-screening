"""HTTP route modules for the versioned candidate-screening API."""

from .analytics import router as analytics_router
from .candidate import router as candidate_router
from .health import router as health_router
from .internal import router as internal_router

__all__ = ["analytics_router", "candidate_router", "health_router", "internal_router"]
