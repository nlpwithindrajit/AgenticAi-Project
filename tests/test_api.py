from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models.travel import TravelRequest

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_plan_trip_returns_a_plan(sample_request: TravelRequest) -> None:
    response = client.post(
        "/plan-trip", json=sample_request.model_dump(mode="json")
    )
    assert response.status_code == 200

    body = response.json()
    assert body["flight_recommendations"]
    assert body["hotel_recommendations"]
    assert len(body["daily_itinerary"]) == sample_request.duration_days
    assert body["budget"]["over_budget"] is False
    assert body["review"]["verdict"] == "PASS"
    assert body["trace_id"]


def test_plan_trip_rejects_an_invalid_request() -> None:
    response = client.post(
        "/plan-trip",
        json={
            "origin": "Mumbai",
            "destinations": ["Tokyo"],
            "departure_date": "2026-10-15",
            "return_date": "2026-10-10",
            "travelers": 2,
            "budget": 200000,
        },
    )
    assert response.status_code == 422
