# AI Travel Planner

A multi-agent LangGraph workflow that turns a structured travel request into
flight options, hotel options, a daily itinerary, restaurants, transport, and a
costed budget — with an explanation of why each option was chosen.

Full design: [`projectIdea.md`](./projectIdea.md). Working agreements:
[`CLAUDE.md`](./CLAUDE.md).

## Status — Milestones 1-4 complete

What is **real**:

- `TravelRequest` / `TripPlan` Pydantic schemas and the shared `TravelState`
- The full LangGraph topology, including **both feedback loops**
- The Review agent's validation checks (dates, coverage, budget, conflicts)
- **Flights, end to end**: Amadeus Self-Service search, IATA resolution,
  explainable filtering and ranking, and an LLM that plans the search and
  writes the recommendation rationale
- **Hotels, end to end**: Amadeus hotel list + pricing + guest ratings, a
  per-destination stay split, distance-from-centre scoring, and the same
  LLM-plans / tool-executes split
- **Activities and restaurants**: Amadeus Tours & Activities and Points of
  Interest, anchored on the hotel we recommended, scheduled one per day
- `POST /plan-trip` and `GET /health` on FastAPI
- Retry-bounded loops, so a bad edit fails loudly instead of hanging

What is **stubbed**: transport only. It invents deterministic inventory
labelled `STUB` so the graph runs end to end with **no API keys at all**.
Nothing user-visible from a stub can be mistaken for a real
search result — and when Amadeus is unconfigured or errors, every search node
falls back to stub inventory *and says so in `TripPlan.errors`*. Milestone 5
puts the Budget, Itinerary and Review agents on real data; the state contract
does not change.

## Quickstart

```bash
uv sync                             # creates .venv and installs from uv.lock

uv run pytest                       # 147 tests, no keys needed (HTTP is mocked)
uv run ruff check .
uv run uvicorn app.main:app --reload
```

`uv run` resolves the environment itself — no activation step, and no way to
accidentally run against a system or conda Python. `uv sync` even fetches the
right interpreter if you don't have it.

<details>
<summary>Without uv (pip)</summary>

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m pytest
.venv/bin/python -m uvicorn app.main:app --reload
```

Keep the `.venv/bin/python -m` prefix — see the troubleshooting note below.
</details>

To search **real flights, hotels, activities and restaurants**, add free
Amadeus Self-Service credentials to `.env`:

```bash
cp .env.example .env
# Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET
```

The Amadeus test tier has limited inventory and non-live prices, so expect
fewer offers than production — switch `AMADEUS_BASE_URL` when you have
production keys. `ANTHROPIC_API_KEY` is optional: without it every agent falls
back to a deterministic search plan and rule-based explanations.

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

### Troubleshooting: `Router.__init__() got an unexpected keyword argument 'on_startup'`

You are running a **different Python than the project's**, almost always a
conda base environment picked up from `PATH` by a bare `uvicorn app.main:app`.
The crash happens inside FastAPI's own `applications.py`, before any project
code loads, which makes an environment problem look like an app bug.

The cause is a FastAPI/Starlette mismatch: FastAPI below 0.141 passes
`on_startup` to Starlette's `Router.__init__`, and Starlette 1.x removed that
argument. Both pinned files here resolve to a working pair.

`uv run …` makes this impossible. If you are not using uv, keep the
`.venv/bin/python -m` prefix, and check what you are actually running:

```bash
uv run python -c "import fastapi, starlette; print(fastapi.__version__, starlette.__version__)"
# expect 0.141.1 1.6.0
```

### Dependency files

`pyproject.toml` is the source of truth. `uv.lock` pins the exact resolution and
is committed, so `uv sync` is reproducible. The two `requirements*.txt` files
are **generated** — for the Docker build in Milestone 8 and for anyone without
uv. Regenerate them after changing a dependency:

```bash
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
uv export --no-hashes --no-emit-project -o requirements-dev.txt
```

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
├── agents/          # flight, hotel, activity, restaurant — reasoning agents
├── tools/           # amadeus.py (transport), flights, hotels, places
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

### Hotels (Milestone 3)

Amadeus splits hotels across three endpoints, and **what each returns decides
what can honestly be scored**:

| Endpoint | Gives us | Does *not* give us |
|---|---|---|
| `hotels/by-city` | id, name, chain, coordinates | star rating, amenity list |
| `hotel-offers` (v3) | prices, room type, free-text room description | structured amenities |
| `hotel-sentiments` | guest rating 0-100 | coverage for every hotel |

Consequences, each handled explicitly rather than papered over:

- **Star ratings are never invented.** `ratings` is a *filter* on `by-city`, not
  a returned field, so `stars` is set only when that filter was honoured. If it
  returns nothing, the agent re-searches unfiltered and clears `stars`.
- **Guest ratings are often missing.** A missing rating stays `None`; it never
  becomes zero, and unrated hotels survive a `min_rating` floor.
- **Amenities are read from room-description text**, so absence means "not
  mentioned", not "not available".

`rank_hotels` therefore scores each factor **only when data exists** and
renormalises the weights over what is present — an unrated hotel is not
punished for Amadeus lacking sentiment data on it. Each option carries its
per-factor breakdown in `score_components`.

| Factor | Weight | Source |
|---|---|---|
| Price | 30% | offer total for the stay |
| Location | 25% | distance from the mean hotel position |
| Rating | 20% | guest sentiment, when available |
| Amenities | 15% | keywords in the room description |
| Traveller preferences | 10% | chain match, confirmed star band |

Amenities the traveller effectively asked for (via `interests`) count double
the baseline ones. Distance is comparative *within a city* — Amadeus exposes no
city-centre coordinate, so the mean hotel position stands in for it.

A multi-destination trip is split into contiguous stays (5 nights over Tokyo +
Kyoto becomes 3 + 2, with no gap between check-out and the next check-in), and
each destination is searched, ranked and costed separately.

### Activities and restaurants (Milestone 4)

Both search around an **anchor point**, resolved in this order:

1. The hotel we actually recommended — things near where the traveller sleeps
   beat things near an abstract centre, and the graph runs the hotel node first.
2. The city centre from Amadeus City Search.

If neither is obtainable the agent raises rather than guessing: searching from
an arbitrary point returns plausible results for the wrong place.

**Activities** come from Tours & Activities, which carries a real price.

| Factor | Weight |
|---|---|
| Rating | 35% |
| Price | 30% |
| Interest match | 25% |
| Proximity to the anchor | 10% |

An activity with no published price is **kept and flagged**
(`cost_is_estimated`) rather than dropped — dropping it would hide free
attractions, and pricing it at zero would understate the budget.

**Restaurants** come from Points of Interest, which returns **no pricing at
all**. That gap is the defining constraint of this agent, and the two halves
are kept strictly apart:

- the **venue** is real, from the provider — `projectIdea.md` §11 is explicit
  that this agent must never invent one;
- the **price** is estimated from the traveller's per-person daily budget and
  trip style, always flagged `price_is_estimated`, and always carries the
  `estimate_basis` sentence explaining where the number came from.

| Factor | Weight |
|---|---|
| Provider relevance (response order) | 40% |
| Dietary match from tags | 30% |
| Proximity to the anchor | 20% |
| Cuisine identifiable | 10% |

Relevance uses **response order**, not the `rank` field, whose scale Amadeus
does not document. When dietary preferences cannot be confirmed from tags, the
plan says so rather than implying the venues were vetted.

Unlike flights and hotels, these two produce a **schedule** — one per day, no
repeats — which is precisely why the Budget agent sums them directly. The full
ranked candidate sets go to `activity_results` / `restaurant_results`.

## Configuration

Copy `.env.example` to `.env`. Nothing in it is required to run the graph —
Amadeus credentials switch flights, hotels, activities and restaurants from
stub to real.

## Roadmap

| # | Milestone | State |
|---|-----------|-------|
| 1 | Foundation — FastAPI, graph skeleton, both loops | done |
| 2 | Flight Agent — Amadeus search, filter, rank | done |
| 3 | Hotel Agent — weighted ranking (price 30 / location 25 / rating 20 / amenities 15 / prefs 10) | done |
| 4 | Activity + Restaurant Agents | done |
| 5 | Budget / Itinerary / Review agents on real data | next |
| 6 | Langfuse instrumentation | |
| 7 | Next.js UI on Vercel | |
| 8 | Docker → ECR → AWS App Runner | |

MVP scope stays narrow: one flight API, one hotel provider, one places API.
Search → filter → rank → recommend. No booking, no auth, no payments.
