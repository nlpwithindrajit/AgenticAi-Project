My recommendation for the first coding sprint: don't start with Vercel or AWS. Start by getting a CLI/API-based LangGraph workflow working with real flight + hotel search, including structured results and Langfuse tracing. Once that core graph is reliable, the FastAPI → Docker → App Runner → Vercel layers become much easier to add.
1. Project goal

Build a web application where a user enters something like:

"Plan a 5-day trip to Tokyo and Kyoto for 2 people from Mumbai, October 10–15, with a budget of ₹2,00,000. Prefer 4-star hotels and direct flights where possible."

The system produces:

✈️ Flight options
🏨 Hotel options
🗺️ Daily itinerary
🍜 Restaurant suggestions
🚆 Local transportation
💰 Estimated total cost
📋 Final trip plan
🔎 Explanation of why particular flights/hotels were selected

And importantly, the application demonstrates:

LangChain + LangGraph + external tools/APIs + Langfuse + Docker + AWS App Runner + Vercel

2. Target architecture

I'd start with this architecture:

                    ┌─────────────────────┐
                    │      Vercel         │
                    │   Next.js Frontend  │
                    └──────────┬──────────┘
                               │
                               │ HTTPS
                               ▼
                    ┌─────────────────────┐
                    │    AWS App Runner   │
                    │                     │
                    │  FastAPI + Docker   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     LangGraph       │
                    │   Travel Workflow   │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
      │ Flight      │   │ Hotel       │   │ Activity    │
      │ Agent       │   │ Agent       │   │ Agent       │
      └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
             │                 │                 │
             ▼                 ▼                 ▼
       Flight APIs        Hotel APIs        Places APIs
             │                 │                 │
             └─────────────────┼─────────────────┘
                               ▼
                       ┌─────────────────┐
                       │  Budget Agent   │
                       └────────┬────────┘
                                ▼
                       ┌─────────────────┐
                       │  Review Agent   │
                       └────────┬────────┘
                                ▼
                       ┌─────────────────┐
                       │ Final Itinerary │
                       └─────────────────┘


                       ┌─────────────────┐
                       │    Langfuse     │
                       │ Traces / Cost   │
                       │ Latency / Eval  │
                       └─────────────────┘
3. The most important part: API strategy

For the first MVP, I'd separate the tools into four categories.

Flights

We need:

Flight Search
Flight Details
Price
Availability

Potential APIs/providers to investigate include Amadeus Self-Service APIs, Duffel, and other flight-search providers.

The important distinction is that some services are excellent for searching flight offers, while others are designed more toward booking/ticketing.

For your first demo, I'd avoid implementing actual booking.

The agent should only:

SEARCH → FILTER → RANK → RECOMMEND

rather than:

SEARCH → PURCHASE

That keeps the project much simpler and safer.

Hotels

Similarly:

Hotel Search
Hotel Availability
Price
Rating
Location
Amenities

For an MVP, we can integrate one hotel provider first rather than trying to aggregate every hotel source.

Activities / restaurants

For this part, we can use a places/search API.

The Activity Agent can search:

tourist attractions
restaurants
museums
parks
shopping
nightlife
local experiences
Maps / transportation

For the first version, don't attempt sophisticated route optimization.

Just calculate:

Airport → Hotel
Hotel → Attraction
Attraction → Restaurant

and provide approximate transportation recommendations.

4. User input schema

The frontend should collect a structured travel request.

class TravelRequest(BaseModel):
    origin: str
    destinations: list[str]


    departure_date: date
    return_date: date


    travelers: int


    budget: float
    currency: str


    hotel_stars: int | None
    preferred_airline: str | None


    direct_flights_only: bool = False


    interests: list[str] = []


    dietary_preferences: list[str] = []


    trip_style: str = "balanced"

Example:

{
  "origin": "Mumbai",
  "destinations": ["Tokyo", "Kyoto"],
  "departure_date": "2026-10-10",
  "return_date": "2026-10-15",
  "travelers": 2,
  "budget": 200000,
  "currency": "INR",
  "hotel_stars": 4,
  "direct_flights_only": false,
  "interests": [
    "food",
    "culture",
    "technology"
  ],
  "trip_style": "balanced"
}

This is much better than giving the initial agent an unstructured paragraph.

5. LangGraph state

The entire workflow should share a common state.

class TravelState(TypedDict):


    request: TravelRequest


    destination_info: dict


    flight_results: list
    hotel_results: list
    activity_results: list
    restaurant_results: list


    flight_recommendations: list
    hotel_recommendations: list


    daily_itinerary: list


    transportation_plan: list


    budget: dict


    review: dict


    final_itinerary: dict


    errors: list

This becomes the backbone of the project.

6. LangGraph workflow

I'd make the graph look like this:

                    START
                      │
                      ▼
             Travel Planner Agent
                      │
                      ▼
              Destination Agent
                      │
          ┌───────────┼───────────┐
          │           │           │
          ▼           ▼           ▼
      Flight       Hotel       Activity
       Agent        Agent        Agent
          │           │           │
          │           │           └──────┐
          │           │                  │
          └───────────┼──────────────────┘
                      ▼
              Restaurant Agent
                      │
                      ▼
                Budget Agent
                      │
                      ▼
             Itinerary Agent
                      │
                      ▼
               Review Agent
                      │
               ┌──────┴──────┐
               │             │
             PASS          FAIL
               │             │
               ▼             ▼
              END       Replanning

This gives us an actual agentic loop, rather than simply calling five LLMs sequentially.

7. Agent responsibilities
Agent 1 — Travel Planner

This is the orchestrator/planning agent.

Input:

User requirements

Output:

{
  "origin": "Mumbai",
  "destinations": ["Tokyo", "Kyoto"],
  "duration": 5,
  "travelers": 2,
  "requirements": {
    "budget": 200000,
    "hotel_stars": 4,
    "interests": ["food", "culture"]
  }
}

It shouldn't search flights itself.

Its job is to create the travel plan requirements.

8. Agent 2 — Flight Agent

This is where tool calling becomes interesting.

The agent gets tools like:

search_flights(
    origin,
    destination,
    departure_date,
    return_date,
    passengers
)


filter_flights(
    flights,
    max_price,
    direct_only
)


rank_flights(
    flights,
    preferences
)

Workflow:

Flight Agent
     │
     ▼
Search Flight API
     │
     ▼
Raw flight results
     │
     ▼
Filter
     │
     ▼
Rank
     │
     ▼
Top 5 flights

I'd have the agent return structured JSON, not prose.

For example:

{
  "recommendations": [
    {
      "airline": "Example Airline",
      "departure": "...",
      "arrival": "...",
      "duration": "...",
      "stops": 0,
      "price": 65000,
      "score": 91
    }
  ]
}
9. Agent 3 — Hotel Agent

Same concept.

Tools:

search_hotels(
    destination,
    check_in,
    check_out,
    guests
)


get_hotel_details(
    hotel_id
)


filter_hotels(
    hotels,
    rating,
    max_price,
    amenities
)


rank_hotels(
    hotels,
    preferences
)

The agent might rank hotels using:

Price           30%
Location        25%
Rating          20%
Amenities       15%
Traveler prefs  10%

This gives us a nice opportunity to explain why the agent selected a hotel.

10. Agent 4 — Activity Agent

This agent gets:

Destination
Dates
Interests
Trip style

and searches for:

Tokyo attractions
Tokyo food
Tokyo culture
Tokyo nightlife
Tokyo experiences

It should produce structured activities:

{
  "activity": "teamLab Borderless",
  "category": "culture",
  "duration_hours": 2,
  "estimated_cost": 3500,
  "location": "...",
  "recommended_day": 2
}
11. Agent 5 — Restaurant Agent

We shouldn't have this agent randomly hallucinate restaurants.

Give it a search tool.

Restaurant Search API
        ↓
Candidate restaurants
        ↓
Filter
        ↓
Dietary preference
        ↓
Price range
        ↓
Distance
        ↓
Rank

For example:

Dinner Day 1
├── Restaurant A
├── Restaurant B
└── Restaurant C
12. Budget Agent

This is where agents become genuinely useful.

The Budget Agent receives:

Flight recommendations
Hotel recommendations
Activities
Restaurants
Transportation

and calculates:

Flights              ₹65,000
Hotels               ₹60,000
Activities            ₹15,000
Restaurants            ₹20,000
Transportation         ₹15,000
--------------------------------
Estimated total      ₹175,000

Then:

Budget = ₹200,000
Estimated = ₹175,000


Remaining = ₹25,000

If the estimated cost exceeds the budget:

Budget Agent
     ↓
OVER BUDGET
     ↓
LangGraph conditional edge
     ↓
Flight Agent
     OR
Hotel Agent
     ↓
Find cheaper alternatives
     ↓
Budget Agent

This is the agentic loop I'd definitely include in the demo.

13. Itinerary Agent

Now we have:

Flights
Hotels
Activities
Restaurants
Budget

The Itinerary Agent converts everything into a day-by-day schedule.

Example:

DAY 1 — Tokyo


10:00  Arrive at Narita
12:00  Transfer to hotel
14:00  Check-in
16:00  Shibuya
19:00  Dinner


DAY 2 — Tokyo


09:00  Asakusa
12:00  Lunch
14:00  teamLab
18:00  Shinjuku
20:00  Dinner

The agent should consider:

geographical proximity
opening hours
travel time
meal times
traveler preferences
budget
14. Review Agent

This is your final quality-control agent.

It checks:

Constraint validation
✓ Correct dates?
✓ Correct number of travelers?
✓ Within budget?
✓ Hotels available?
✓ Flight dates correct?
✓ Activities compatible with itinerary?
Logical validation
✗ Two attractions scheduled at the same time
✗ Restaurant closed that day
✗ Hotel checkout before activity
✗ Impossible transportation time

If everything passes:

REVIEW → PASS → END

Otherwise:

REVIEW → FAIL
          ↓
       Planner
          ↓
      Replanning
15. Langfuse observability

This is where your project becomes much more interesting than a normal chatbot.

Every request should create a Langfuse trace:

Trace
│
├── Travel Planner
│   └── LLM call
│
├── Flight Agent
│   ├── LLM call
│   ├── Flight API
│   ├── LLM ranking
│   └── Flight API
│
├── Hotel Agent
│   ├── LLM call
│   └── Hotel API
│
├── Activity Agent
│   └── Places API
│
├── Budget Agent
│   └── LLM call
│
├── Itinerary Agent
│   └── LLM call
│
└── Review Agent
    └── LLM call

Then you can show a demo dashboard with:

Travel Request #124


Total latency:      18.4 sec
LLM calls:          11
Tool calls:          9
Tokens:          14,821
Estimated cost:   $0.18


Agent performance


Planner       1.2s
Flights       5.4s
Hotels        4.1s
Activities    3.2s
Budget        1.1s
Itinerary     2.3s
Review        1.1s

That's an excellent portfolio/demo feature.

16. Vercel frontend

I'd build a fairly simple UI initially.

Page 1 — Trip Request
┌───────────────────────────────────────────┐
│          AI TRAVEL PLANNER                │
│                                           │
│ From:       [ Mumbai              ]       │
│ Destination:[ Tokyo + Kyoto       ]       │
│                                           │
│ Departure:  [ 10 Oct 2026         ]      │
│ Return:     [ 15 Oct 2026         ]      │
│                                           │
│ Travelers:  [ 2                   ]       │
│ Budget:     [ ₹2,00,000           ]       │
│                                           │
│ Interests:                                │
│ ☑ Food  ☑ Culture  ☐ Shopping             │
│                                           │
│        [ PLAN MY TRIP ]                   │
└───────────────────────────────────────────┘

Then show an agent execution screen.

17. Agent execution UI

This could be one of the coolest parts of the demo.

Planning your trip...


✓ Understanding travel requirements
✓ Searching flights
⟳ Searching hotels...
○ Finding activities
○ Finding restaurants
○ Calculating budget
○ Building itinerary
○ Reviewing itinerary

Then transition to:

Your Japan Trip
──────────────────────────


✈ Flights
Best option
₹65,000
Non-stop
Mumbai → Tokyo


🏨 Hotels
4-star hotel
₹60,000
Shinjuku


💰 Budget
₹175,000 / ₹200,000


🗓 Itinerary
Day 1 — Tokyo
Day 2 — Tokyo
Day 3 — Tokyo → Kyoto
Day 4 — Kyoto
Day 5 — Kyoto → Tokyo
18. Backend project structure

I'd organize the Python project like this:

travel-agent/
│
├── app/
│   ├── main.py
│   │
│   ├── graph/
│   │   ├── state.py
│   │   ├── graph.py
│   │   └── nodes.py
│   │
│   ├── agents/
│   │   ├── planner.py
│   │   ├── flight.py
│   │   ├── hotel.py
│   │   ├── activity.py
│   │   ├── restaurant.py
│   │   ├── budget.py
│   │   ├── itinerary.py
│   │   └── reviewer.py
│   │
│   ├── tools/
│   │   ├── flights.py
│   │   ├── hotels.py
│   │   ├── places.py
│   │   └── maps.py
│   │
│   ├── services/
│   │   ├── langfuse.py
│   │   └── currency.py
│   │
│   └── models/
│       └── travel.py
│
├── tests/
│
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
19. Deployment architecture

The production deployment can be:

GitHub
   │
   ▼
GitHub Actions
   │
   ▼
Docker Build
   │
   ▼
Amazon ECR
   │
   ▼
AWS App Runner
   │
   ▼
FastAPI

Frontend:

GitHub
   │
   ▼
Vercel
   │
   ▼
Next.js

Communication:

Vercel
   │
   │ HTTPS
   ▼
AWS App Runner
   │
   ├── LLM API
   ├── Flight API
   ├── Hotel API
   ├── Places API
   └── Langfuse
20. MVP vs Phase 2

I strongly recommend not implementing everything initially.

MVP — Version 1

Build only:

User
 ↓
Planner
 ↓
Flight Agent
 ↓
Hotel Agent
 ↓
Activity Agent
 ↓
Budget Agent
 ↓
Itinerary Agent
 ↓
Review Agent
 ↓
Final Result

Use one flight API, one hotel source and one places API.

No booking.

No user accounts.

No payment.

No complicated memory.

No vector database initially.

Version 2

Add:

Parallel agent execution
        +
LangGraph replanning
        +
Langfuse evaluation
        +
Currency conversion
        +
Maps
Version 3

Add:

Real-time availability
        +
Price change detection
        +
User preferences
        +
Trip memory
        +
Saved trips
        +
Authentication
21. The actual development roadmap

I'd break the project into 8 milestones.

Milestone 1 — Foundation
Python project
FastAPI
LangChain
LangGraph
Environment configuration
Basic /plan-trip endpoint

Deliverable: Simple LangGraph workflow.

Milestone 2 — Flight Agent
Select flight API
Build API client
Create LangChain tool
Flight search agent
Structured flight results
Error handling

Deliverable: User can search and rank flights.

Milestone 3 — Hotel Agent
Hotel API
Search tool
Filtering
Ranking
Structured output

Deliverable: Flights + hotels.

Milestone 4 — Activities & Restaurants

Add:

Activity Agent
Restaurant Agent

Deliverable: Complete travel inventory.

Milestone 5 — Agentic Planning

Add:

Budget Agent
Itinerary Agent
Review Agent

Then introduce the important LangGraph loop:

Review
  │
  ├── PASS → Final
  │
  └── FAIL → Replan → Search again

Deliverable: Genuine multi-agent workflow.

Milestone 6 — Langfuse

Instrument:

traces
generations
tool calls
latency
token usage
errors
cost
evaluation scores

Deliverable: Observable agent system.

Milestone 7 — Vercel UI

Build:

Trip form
Streaming agent status
Flight cards
Hotel cards
Budget visualization
Day-by-day itinerary
Map integration

Deliverable: Usable web application.

Milestone 8 — AWS Deployment

Build:

Docker
 ↓
ECR
 ↓
App Runner

Then:

Vercel → App Runner → LangGraph

Add:

environment variables
secrets
health check
logging
CI/CD
production Langfuse

Deliverable: Public production-style demo.

22. One important architectural choice

I would not initially create:

Planner Agent
Flight Agent
Hotel Agent
Activity Agent
Restaurant Agent
Budget Agent
Review Agent

as seven completely independent autonomous agents.

Instead, make a distinction between reasoning agents and tool nodes.

For example:

                    LangGraph
                       │
                 Planner Agent
                       │
           ┌───────────┼───────────┐
           ▼           ▼           ▼
      Flight Agent  Hotel Agent  Activity Agent
           │           │           │
           ▼           ▼           ▼
       Flight Tool  Hotel Tool  Places Tool

The LLM decides how to use the tool, while the actual API interaction remains deterministic Python code.

That's a much better production architecture.

23. Final end-to-end workflow

The finished system should ultimately look like this:

USER
 │
 ▼
Vercel / Next.js
 │
 ▼
FastAPI
 │
 ▼
┌─────────────────────────────┐
│        LANGGRAPH            │
│                             │
│  Travel Planner             │
│       │                     │
│       ├─────────────┐       │
│       ▼             ▼       │
│   Flight Agent   Hotel Agent│
│       │             │       │
│   Flight API     Hotel API  │
│       │             │       │
│       └──────┬──────┘       │
│              ▼              │
│       Activity Agent        │
│              │              │
│         Places API          │
│              │              │
│              ▼              │
│       Restaurant Agent      │
│              │              │
│              ▼              │
│         Budget Agent        │
│              │              │
│              ▼              │
│        Itinerary Agent      │
│              │              │
│              ▼              │
│         Review Agent        │
│           │       │         │
│          PASS    FAIL       │
│           │       │         │
│           │    Replan       │
│           │       │         │
│           └───────┘         │
└──────────────┬──────────────┘
               │
               ▼
        Final Trip Plan
               │
               ▼
           Vercel UI




       ┌─────────────────┐
       │     Langfuse    │
       │                 │
       │ Traces          │
       │ LLM Calls       │
       │ Tool Calls      │
       │ Tokens          │
       │ Cost            │
       │ Latency         │
       │ Evaluations     │
       └─────────────────┘

My recommendation for the first coding sprint: don't start with Vercel or AWS. Start by getting a CLI/API-based LangGraph workflow working with real flight + hotel search, including structured results and Langfuse tracing. Once that core graph is reliable, the FastAPI → Docker → App Runner → Vercel layers become much easier to add.