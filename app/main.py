"""FastAPI entry point for the AI travel planner."""

from __future__ import annotations

import logging

# Must run before FastAPI is constructed: on a mismatched environment the
# constructor throws from deep inside FastAPI, and this turns that into a
# message that names the problem. See app/env_check.py.
from app.env_check import check_runtime

check_runtime()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.graph.graph import plan_trip  # noqa: E402
from app.models.travel import TravelRequest, TripPlan  # noqa: E402
from app.services.langfuse import new_trace_id, span  # noqa: E402

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Travel Planner",
    version="0.1.0",
    description="Multi-agent LangGraph trip planning (search -> rank -> recommend).",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness probe — also used as the App Runner health check."""
    return {
        "status": "ok",
        "environment": settings.environment,
        "langfuse_enabled": settings.langfuse_enabled,
    }


@app.post("/plan-trip", response_model=TripPlan)
def plan_trip_endpoint(request: TravelRequest) -> TripPlan:
    """Run the LangGraph workflow for one structured travel request."""
    trace_id = new_trace_id()
    logger.info(
        "planning trip %s -> %s (trace %s)",
        request.origin,
        request.destinations,
        trace_id,
    )
    try:
        with span("plan-trip", trace_id=trace_id):
            return plan_trip(request, trace_id=trace_id)
    except Exception as exc:
        logger.exception("trip planning failed (trace %s)", trace_id)
        raise HTTPException(
            status_code=500, detail=f"trip planning failed: {exc}"
        ) from exc
