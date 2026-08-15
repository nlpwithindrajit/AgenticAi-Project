# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An AI travel planner: the user submits a structured travel request (origin, destinations, dates, travelers, budget, preferences) and a multi-agent LangGraph workflow produces flight options, hotel options, a daily itinerary, restaurant suggestions, transportation, cost estimates, and an explanation of the choices.

The full plan lives in `projectIdea.md` — read it before making architectural decisions. This is currently a greenfield repo: no application code exists yet.

**Stack:** Python, LangChain + LangGraph, FastAPI, Langfuse (observability), Docker → Amazon ECR → AWS App Runner (backend), Next.js on Vercel (frontend).

## Build Order (important)

Do NOT start with the frontend or deployment. The agreed first sprint is a CLI/API-driven LangGraph workflow with real flight + hotel search, structured results, and Langfuse tracing. FastAPI → Docker → App Runner → Vercel layers come after the core graph is reliable.

Milestone sequence: foundation (FastAPI skeleton + basic `/plan-trip` endpoint + simple graph) → Flight Agent → Hotel Agent → Activity/Restaurant Agents → Budget/Itinerary/Review Agents with the replanning loop → Langfuse instrumentation → Vercel UI → AWS deployment.

MVP scope is deliberately narrow: one flight API, one hotel provider, one places API. No booking (search → filter → rank → recommend only), no auth, no payments, no vector DB, no memory.

## Architecture

### LangGraph workflow

A single shared `TravelState` (TypedDict) is the backbone — every node reads/writes it. It carries the `TravelRequest` (Pydantic model, see `projectIdea.md` §4), raw search results, recommendations, itinerary, budget, review verdict, and errors.

Node order: Travel Planner → Destination → Flight / Hotel / Activity agents → Restaurant → Budget → Itinerary → Review. The Review agent gates the output: PASS → END, FAIL → conditional edge back to replanning. The Budget agent has its own loop: if estimated cost exceeds budget, a conditional edge routes back to the Flight or Hotel agent to find cheaper alternatives. These loops are the point of the project — keep them, don't flatten the graph into a linear LLM chain.

### Reasoning agents vs. tool nodes

Key design decision (`projectIdea.md` §22): agents are NOT independent autonomous LLMs. The LLM decides how to use tools; actual API interaction is deterministic Python code in `app/tools/`. Agents return structured JSON, never prose. The Restaurant/Activity agents must draw from search-tool results, never hallucinate venues.

### Planned backend layout

```
app/
├── main.py          # FastAPI entry
├── graph/           # state.py, graph.py, nodes.py — LangGraph wiring
├── agents/          # planner, flight, hotel, activity, restaurant, budget, itinerary, reviewer
├── tools/           # flights, hotels, places, maps — deterministic API clients
├── services/        # langfuse.py, currency.py
└── models/          # travel.py — Pydantic schemas (TravelRequest etc.)
```

### Observability

Every request creates one Langfuse trace with nested spans per agent, covering LLM calls, tool calls, latency, tokens, and cost. Instrument new agents/tools as they are added, not retroactively.

## Conventions

- Frontend collects a structured `TravelRequest`, not free text — agents consume the schema.
- Flight/hotel ranking should be explainable (e.g., hotel score weights: price 30%, location 25%, rating 20%, amenities 15%, preferences 10%).
- Git identity for this repo is `nlpwithindrajit <nlpwithindrajit@gmail.com>` (personal account, set in local git config). Pushes require switching the GitHub CLI account: `gh auth switch --user nlpwithindrajit`, then switch back to `Indrajit-Singh_expedEMU` afterward.
