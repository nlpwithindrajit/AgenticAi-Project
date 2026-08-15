# AI Travel Planner

A multi-agent LangGraph workflow that turns a structured travel request into
flight options, hotel options, a daily itinerary, restaurants, transport, and a
costed budget — with an explanation of why each option was chosen.

Full design: [`projectIdea.md`](./projectIdea.md). Working agreements:
[`CLAUDE.md`](./CLAUDE.md).

## Status — Milestone 1 (Foundation) complete

What is **real**:

- `TravelRequest` / `TripPlan` Pydantic schemas and the shared `TravelState`
- The full LangGraph topology, including **both feedback loops**
- The Review agent's validation checks (dates, coverage, budget, conflicts)
- `POST /plan-trip` and `GET /health` on FastAPI
- Retry-bounded loops, so a bad edit fails loudly instead of hanging

What is **stubbed**: every search node in `app/graph/nodes.py`. They invent
deterministic inventory labelled `STUB` so the graph runs end to end with **no
API keys at all**. Nothing user-visible from a stub can be mistaken for a real
search result. Milestones 2–5 replace each stub body with a reasoning agent
(`app/agents/`) driving a deterministic tool client (`app/tools/`); the state
contract does not change.

## Quickstart

```bash
uv venv --python 3.11
uv pip install -r requirements-dev.txt

.venv/bin/python -m pytest          # 15 tests, no keys needed
.venv/bin/python -m ruff check .
.venv/bin/python -m uvicorn app.main:app --reload
```

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

## Configuration

Copy `.env.example` to `.env`. Nothing in it is required at Milestone 1.

## Roadmap

| # | Milestone | State |
|---|-----------|-------|
| 1 | Foundation — FastAPI, graph skeleton, both loops | done |
| 2 | Flight Agent — real search, filter, rank | next |
| 3 | Hotel Agent — weighted ranking (price 30 / location 25 / rating 20 / amenities 15 / prefs 10) | |
| 4 | Activity + Restaurant Agents | |
| 5 | Budget / Itinerary / Review agents on real data | |
| 6 | Langfuse instrumentation | |
| 7 | Next.js UI on Vercel | |
| 8 | Docker → ECR → AWS App Runner | |

MVP scope stays narrow: one flight API, one hotel provider, one places API.
Search → filter → rank → recommend. No booking, no auth, no payments.
