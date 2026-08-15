"use client";

import { useRef, useState } from "react";
import { API_BASE, ApiError } from "@/lib/api";
import type { TripPlan } from "@/lib/types";

interface ChatResponse {
  reply: string;
  draft: Record<string, unknown>;
  request: unknown | null;
  missing: string[];
  ready: boolean;
  used_llm: boolean;
  plan: TripPlan | null;
}

interface Turn {
  who: "you" | "planner";
  text: string;
}

const EXAMPLE =
  "Plan a 5-day trip to Tokyo and Kyoto for 2 people from Mumbai, " +
  "October 10-15, with a budget of ₹2,00,000. Prefer 4-star hotels.";

export interface ChatProps {
  onPlan: (plan: TripPlan) => void;
}

/**
 * A conversational front door. It does not bypass the structured request —
 * the backend turns each message into a `TravelRequest` and runs the same
 * graph, so the agents still consume the schema.
 */
export default function Chat({ onPlan }: ChatProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ruleBased, setRuleBased] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;

    setTurns((t) => [...t, { who: "you", text: trimmed }]);
    setMessage("");
    setBusy(true);
    setError(null);

    try {
      const response = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed, draft }),
      });
      if (!response.ok) {
        throw new ApiError(`The planner returned ${response.status}.`, response.status);
      }
      const body: ChatResponse = await response.json();

      setDraft(body.draft);
      setRuleBased(!body.used_llm);
      setTurns((t) => [...t, { who: "planner", text: body.reply }]);
      if (body.plan) onPlan(body.plan);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : `Could not reach the planner at ${API_BASE}.`,
      );
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>Describe your trip</strong>
        {ruleBased && (
          <span
            className="badge est"
            title="ANTHROPIC_API_KEY is not set, so this is rule-based parsing rather than a model. It reads clear phrasings only."
          >
            rule-based
          </span>
        )}
      </div>

      {turns.length === 0 && (
        <p className="small muted" style={{ marginTop: 8 }}>
          Try:{" "}
          <button
            type="button"
            className="chip"
            onClick={() => send(EXAMPLE)}
            disabled={busy}
          >
            {EXAMPLE.slice(0, 62)}…
          </button>
        </p>
      )}

      {turns.length > 0 && (
        <div style={{ margin: "12px 0" }}>
          {turns.map((turn, i) => (
            <div key={i} className="slot" style={{ gridTemplateColumns: "70px 1fr" }}>
              <span className="muted small">
                {turn.who === "you" ? "You" : "Planner"}
              </span>
              <span>{turn.text}</span>
            </div>
          ))}
          {busy && (
            <div className="slot" style={{ gridTemplateColumns: "70px 1fr" }}>
              <span className="muted small">Planner</span>
              <span>
                <span className="spinner" aria-hidden />
                thinking…
              </span>
            </div>
          )}
        </div>
      )}

      <form
        className="row"
        style={{ marginTop: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          void send(message);
        }}
      >
        <input
          ref={inputRef}
          type="text"
          value={message}
          placeholder="e.g. 4 days in Lisbon from London in May, 2 people, £1500"
          onChange={(e) => setMessage(e.target.value)}
          disabled={busy}
          aria-label="Describe your trip"
        />
        <button className="primary" type="submit" disabled={busy || !message.trim()}>
          Send
        </button>
      </form>

      {error && (
        <p className="small" style={{ color: "var(--bad)", marginBottom: 0 }}>
          {error}
        </p>
      )}
    </div>
  );
}
