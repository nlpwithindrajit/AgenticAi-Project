# AI Travel Planner

A multi-agent LangGraph workflow that turns a structured travel request into
flight options, hotel options, a daily itinerary, restaurants, transport, and a
costed budget — with an explanation of why each option was chosen.

Full design: [`projectIdea.md`](./projectIdea.md). Working agreements:
[`CLAUDE.md`](./CLAUDE.md).

## Status — Milestones 1 & 2 complete

What is **real**:

- `TravelRequest` / `TripPlan` Pydantic schemas and the shared `TravelState`
- The full LangGraph topology, including **both feedback loops**
- The Review agent's validation checks (dates, coverage, budget, conflicts)
- **Flights, end to end**: Amadeus Self-Service search, IATA resolution,
  explainable filtering and ranking, and an LLM that plans the search and
  writes the recommendation rationale
- `POST /plan-trip` and `GET /health` on FastAPI
- Retry-bounded loops, so a bad edit fails loudly instead of hanging

What is **stubbed**: hotels, activities, restaurants and transport. They invent
deterministic inventory labelled `STUB` so the graph runs end to end with **no
API keys at all**. Nothing user-visible from a stub can be mistaken for a real
search result — and when Amadeus is unconfigured or errors, the flight node
falls back to stub inventory *and says so in `TripPlan.errors`*. Milestones 3–5
replace each remaining stub with a reasoning agent (`app/agents/`) driving a
deterministic tool client (`app/tools/`); the state contract does not change.

## Quickstart

```bash
uv venv --python 3.11
uv pip install -r requirements-dev.txt

.venv/bin/python -m pytest          # 56 tests, no keys needed (HTTP is mocked)
.venv/bin/python -m ruff check .
.venv/bin/python -m uvicorn app.main:app --reload
```

To search **real flights**, add free Amadeus Self-Service credentials to `.env`:

```bash
cp .env.example .env
# Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET
```

The Amadeus test tier has limited inventory and non-live prices, so expect
fewer offers than production — switch `AMADEUS_BASE_URL` when you have
production keys. `ANTHROPIC_API_KEY` is optional: without it the Flight agent
falls back to a deterministic search plan and rule-based explanations.

Then:

```bash
curl -X POST http://127.0.0.1:8000/plan-trip \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": "Mumbai",
    "destinations": ["Tokyo", "Kyoto"],
    "departure_date": "2026-10-10",
    "return_date": "2026-10-15",
    "travelers": 2,
    "budget": 200000,
    "currency": "INR",
    "hotel_stars": 4,
    "interests": ["food", "culture"]
  }'
```

Interactive docs at <http://127.0.0.1:8000/docs>.

## The graph

```
START -> planner -> destination -> flight -> hotel -> activity
      -> restaurant -> transportation -> budget
                                          |
                      over budget? -> replan_budget -> flight | hotel
                                          |
                                      itinerary -> review
                                                    |
                                          PASS -> END
                                          FAIL -> replan -> planner
```

The two loops are the point of the project — do not flatten the graph into a
linear LLM chain. Both are bounded (`MAX_BUDGET_RETRIES`, `MAX_REVIEW_RETRIES`
in `app/graph/state.py`).

The stub costing model deliberately starts ~10% over budget so a default
request exercises the budget loop and converges. `TripPlan.errors` records each
time a loop fired.

## Layout

```
app/
├── main.py          # FastAPI entry: /health, /plan-trip
├── config.py        # env settings; every integration optional
├── graph/           # state.py, nodes.py, graph.py — LangGraph wiring
├── agents/          # reasoning agents            (Milestone 2+)
├── tools/           # deterministic API clients   (Milestone 2+)
├── services/        # langfuse.py — trace seam    (Milestone 6 fills it in)
└── models/          # travel.py — Pydantic schemas
tests/
```

Agents decide *how* to use tools; the tools do the deterministic API work and
return structured data. No LLM calls belong in `app/tools/`.

### Flights (Milestone 2)

`app/tools/flights.py` owns OAuth2, IATA lookup, the search call, normalisation
and scoring. `app/agents/flight.py` owns the two judgement calls: which search
parameters to use, and how to explain the winner. The agent can only recommend
offers the provider actually returned.

A round trip is **one** Amadeus offer carrying two itineraries at a single
price, so it maps to one `FlightOption` with an `outbound` and an `inbound`
slice. Recommendations are therefore *alternatives*, not legs — the Budget
agent costs only the top-ranked offer.

Ranking weights, all explainable and recorded in each option's `rationale`:

| Factor | Weight |
|---|---|
| Price | 40% |
| Total duration | 25% |
| Stops | 20% |
| Departure time (06:00–20:00 preferred) | 10% |
| Preferred airline match | 5% |

Scores are relative to the candidate set: 100 means "best of what was
available", not an absolute quality rating. Hard constraints are **relaxed
rather than emptied** — if `direct_flights_only` eliminates every option, the
connecting flights come back with a note instead of an empty result.

Known MVP limits, all surfaced in `TripPlan.errors` rather than hidden:
multi-city routing is priced as a round trip to the first destination, and the
Amadeus test tier returns limited inventory.

## Configuration

Copy `.env.example` to `.env`. Nothing in it is required at Milestone 1.

## Roadmap

| # | Milestone | State |
|---|-----------|-------|
| 1 | Foundation — FastAPI, graph skeleton, both loops | done |
| 2 | Flight Agent — Amadeus search, filter, rank | done |
| 3 | Hotel Agent — weighted ranking (price 30 / location 25 / rating 20 / amenities 15 / prefs 10) | next |
| 4 | Activity + Restaurant Agents | |
| 5 | Budget / Itinerary / Review agents on real data | |
| 6 | Langfuse instrumentation | |
| 7 | Next.js UI on Vercel | |
| 8 | Docker → ECR → AWS App Runner | |

MVP scope stays narrow: one flight API, one hotel provider, one places API.
Search → filter → rank → recommend. No booking, no auth, no payments.
