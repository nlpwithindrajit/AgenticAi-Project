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
from pydantic import BaseModel, Field  # noqa: E402

from app.agents.intake import IntakeAgent, TripDraft  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.graph.graph import plan_trip  # noqa: E402
from app.models.travel import TravelRequest, TripPlan  # noqa: E402
from app.services.langfuse import (  # noqa: E402
    new_trace_id,
    score,
    trace,
    update_current,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Travel Planner",
    version="0.1.0",
    description="Multi-agent LangGraph trip planning (search -> rank -> recommend).",
)

if settings.environment != "local" and settings.cors_is_default:
    # A CORS misconfiguration presents as "the UI is broken" with a healthy
    # API and no server-side error, which is expensive to diagnose. Say it now.
    logger.warning(
        "ALLOWED_ORIGINS is still the localhost default in environment %r — "
        "a deployed browser UI will be blocked. Set it to the UI's URL.",
        settings.environment,
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
        # One trace per request; every agent, tool and LLM call nests under it.
        with trace(
            "plan-trip",
            trace_id=trace_id,
            input=request.model_dump(mode="json"),
            metadata={"trace_id": trace_id, "travelers": request.travelers},
            tags=["plan-trip", request.trip_style],
        ):
            plan = plan_trip(request, trace_id=trace_id)
            update_current(output=_trace_summary(plan))
            _score(plan)
            return plan
    except Exception as exc:
        logger.exception("trip planning failed (trace %s)", trace_id)
        raise HTTPException(
            status_code=500, detail=f"trip planning failed: {exc}"
        ) from exc


def _trace_summary(plan: TripPlan) -> dict[str, object]:
    """A compact trace output — the whole plan would bury the useful bits."""
    return {
        "review": plan.review.verdict if plan.review else None,
        "estimated_total": plan.budget.estimated_total if plan.budget else None,
        "budget": plan.budget.budget if plan.budget else None,
        "over_budget": plan.budget.over_budget if plan.budget else None,
        "flights": len(plan.flight_recommendations),
        "hotels": len(plan.hotel_recommendations),
        "activities": len(plan.activities),
        "restaurants": len(plan.restaurants),
        "days": len(plan.daily_itinerary),
        "notes": plan.errors,
    }


def _score(plan: TripPlan) -> None:
    """Evaluation scores on the trace, so quality is visible over time."""
    if plan.review is not None:
        score(
            "review_passed",
            1.0 if plan.review.verdict == "PASS" else 0.0,
            comment="; ".join(i.detail for i in plan.review.issues) or None,
        )
    if plan.budget is not None and plan.budget.budget > 0:
        # Below 1.0 is within budget; above means the loop could not converge.
        score(
            "budget_used",
            round(plan.budget.estimated_total / plan.budget.budget, 4),
            comment=f"{plan.budget.estimated_total} of {plan.budget.budget}",
        )


class ChatRequest(BaseModel):
    """One turn of the conversational front door."""

    message: str = Field(min_length=1, max_length=2000)
    draft: TripDraft | None = Field(
        default=None,
        description="The draft from the previous turn. Omit on the first turn.",
    )
    plan_when_ready: bool = Field(
        default=True,
        description="Run the planner as soon as the request is complete.",
    )


class ChatResponse(BaseModel):
    """What the caller needs to render the next turn."""

    reply: str
    draft: TripDraft
    request: TravelRequest | None = None
    missing: list[str] = Field(default_factory=list)
    ready: bool = False
    used_llm: bool = False
    plan: TripPlan | None = None


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(body: ChatRequest) -> ChatResponse:
    """Conversational intake that produces a structured TravelRequest.

    The agents still consume the schema — this only builds one. Nothing is
    guessed into a required field: a missing budget becomes a question, not an
    invented number that would then drive every search.
    """
    trace_id = new_trace_id()
    try:
        with trace(
            "chat",
            trace_id=trace_id,
            input={"message": body.message},
            tags=["chat"],
        ):
            result = IntakeAgent().read(body.message, draft=body.draft)

            plan: TripPlan | None = None
            if result.request is not None and body.plan_when_ready:
                plan = plan_trip(result.request, trace_id=trace_id)
                _score(plan)

            return ChatResponse(
                reply=result.reply,
                draft=result.draft,
                request=result.request,
                missing=result.missing,
                ready=result.request is not None,
                used_llm=result.used_llm,
                plan=plan,
            )
    except Exception as exc:
        logger.exception("chat turn failed (trace %s)", trace_id)
        raise HTTPException(status_code=500, detail=f"chat failed: {exc}") from exc
