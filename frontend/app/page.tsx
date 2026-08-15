"use client";

import { useState } from "react";
import Chat from "./Chat";
import Results from "./Results";
import TripForm from "./TripForm";
import { API_BASE, ApiError, planTrip } from "@/lib/api";
import type { TravelRequest, TripPlan } from "@/lib/types";

/**
 * The agents the graph runs, in order.
 *
 * Shown while a request is in flight so the wait is legible. Deliberately
 * *not* presented as live per-agent progress: `/plan-trip` is a single
 * request/response, so the UI cannot know which agent is running. Faking that
 * would be theatre. Real per-agent ticks need a streaming endpoint.
 */
const PIPELINE = [
  "Understanding your requirements",
  "Searching flights",
  "Searching hotels",
  "Finding activities",
  "Finding restaurants",
  "Costing the budget",
  "Building the itinerary",
  "Reviewing the plan",
];

export default function Home() {
  const [plan, setPlan] = useState<TripPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"chat" | "form">("chat");

  async function run(request: TravelRequest) {
    setBusy(true);
    setError(null);
    setPlan(null);
    try {
      setPlan(await planTrip(request));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Something went wrong planning.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="chips" style={{ marginBottom: 16 }}>
        <button
          type="button"
          className="chip"
          aria-pressed={mode === "chat"}
          onClick={() => setMode("chat")}
        >
          Describe it
        </button>
        <button
          type="button"
          className="chip"
          aria-pressed={mode === "form"}
          onClick={() => setMode("form")}
        >
          Fill in a form
        </button>
      </div>

      {mode === "chat" ? (
        <Chat
          onPlan={(p) => {
            setError(null);
            setPlan(p);
          }}
        />
      ) : (
        <TripForm onSubmit={run} busy={busy} />
      )}

      {busy && (
        <div className="card">
          <p style={{ margin: "0 0 10px" }}>
            <span className="spinner" aria-hidden />
            Planning your trip…
          </p>
          <ul className="notes">
            {PIPELINE.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
          <p className="small muted" style={{ marginBottom: 0 }}>
            The budget and review loops may re-run some of these.
          </p>
        </div>
      )}

      {error && (
        <div className="error">
          <strong>Could not plan the trip.</strong>
          <p style={{ margin: "6px 0 0" }}>{error}</p>
          <p className="small muted" style={{ margin: "8px 0 0" }}>
            API: <code>{API_BASE}</code>
          </p>
        </div>
      )}

      {plan && !busy && <Results plan={plan} />}
    </>
  );
}
