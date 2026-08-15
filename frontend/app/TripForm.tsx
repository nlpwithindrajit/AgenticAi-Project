"use client";

import { useState } from "react";
import type { TravelRequest, TripStyle } from "@/lib/types";

const INTERESTS = [
  "food",
  "culture",
  "history",
  "nature",
  "technology",
  "shopping",
  "nightlife",
  "adventure",
  "art",
  "wellness",
];

const DIETARY = ["vegetarian", "vegan", "halal", "kosher", "gluten_free"];

function isoDaysFromNow(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

export interface TripFormProps {
  onSubmit: (request: TravelRequest) => void;
  busy: boolean;
}

/**
 * Collects a *structured* TravelRequest rather than free text — the agents
 * consume the schema directly, so there is no parsing step to get wrong.
 */
export default function TripForm({ onSubmit, busy }: TripFormProps) {
  const [origin, setOrigin] = useState("Mumbai");
  const [destinations, setDestinations] = useState("Tokyo, Kyoto");
  const [departure, setDeparture] = useState(isoDaysFromNow(60));
  const [ret, setRet] = useState(isoDaysFromNow(65));
  const [travelers, setTravelers] = useState(2);
  const [budget, setBudget] = useState(200000);
  const [currency, setCurrency] = useState("INR");
  const [stars, setStars] = useState<number | "">(4);
  const [direct, setDirect] = useState(false);
  const [style, setStyle] = useState<TripStyle>("balanced");
  const [interests, setInterests] = useState<string[]>(["food", "culture"]);
  const [dietary, setDietary] = useState<string[]>([]);

  const destinationList = destinations
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);

  const datesValid = ret >= departure;
  const valid =
    origin.trim().length > 0 &&
    destinationList.length > 0 &&
    datesValid &&
    travelers >= 1 &&
    budget > 0;

  function toggle(
    value: string,
    list: string[],
    set: (next: string[]) => void,
  ): void {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || busy) return;
    onSubmit({
      origin: origin.trim(),
      destinations: destinationList,
      departure_date: departure,
      return_date: ret,
      travelers,
      budget,
      currency: currency.trim().toUpperCase(),
      hotel_stars: stars === "" ? null : Number(stars),
      direct_flights_only: direct,
      interests,
      dietary_preferences: dietary,
      trip_style: style,
    });
  }

  return (
    <form className="card" onSubmit={submit}>
      <div className="grid">
        <div>
          <label htmlFor="origin">From</label>
          <input
            id="origin"
            type="text"
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
            required
          />
        </div>
        <div style={{ gridColumn: "span 2" }}>
          <label htmlFor="destinations">
            Destinations <span className="muted">(comma separated)</span>
          </label>
          <input
            id="destinations"
            type="text"
            value={destinations}
            onChange={(e) => setDestinations(e.target.value)}
            required
          />
        </div>
        <div>
          <label htmlFor="departure">Departure</label>
          <input
            id="departure"
            type="date"
            value={departure}
            onChange={(e) => setDeparture(e.target.value)}
            required
          />
        </div>
        <div>
          <label htmlFor="return">Return</label>
          <input
            id="return"
            type="date"
            value={ret}
            min={departure}
            onChange={(e) => setRet(e.target.value)}
            required
          />
          {!datesValid && (
            <p className="small" style={{ color: "var(--bad)", margin: "5px 0 0" }}>
              Return must not be before departure.
            </p>
          )}
        </div>
        <div>
          <label htmlFor="travelers">Travellers</label>
          <input
            id="travelers"
            type="number"
            min={1}
            value={travelers}
            onChange={(e) => setTravelers(Number(e.target.value))}
          />
        </div>
        <div>
          <label htmlFor="budget">Total budget</label>
          <input
            id="budget"
            type="number"
            min={1}
            step={1000}
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
          />
        </div>
        <div>
          <label htmlFor="currency">Currency</label>
          <input
            id="currency"
            type="text"
            maxLength={3}
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="stars">Hotel stars</label>
          <select
            id="stars"
            value={stars}
            onChange={(e) =>
              setStars(e.target.value === "" ? "" : Number(e.target.value))
            }
          >
            <option value="">No preference</option>
            {[1, 2, 3, 4, 5].map((s) => (
              <option key={s} value={s}>
                {s} star
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="style">Pace</label>
          <select
            id="style"
            value={style}
            onChange={(e) => setStyle(e.target.value as TripStyle)}
          >
            <option value="relaxed">Relaxed</option>
            <option value="balanced">Balanced</option>
            <option value="packed">Packed</option>
          </select>
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <label>Interests</label>
        <div className="chips">
          {INTERESTS.map((i) => (
            <button
              type="button"
              key={i}
              className="chip"
              aria-pressed={interests.includes(i)}
              onClick={() => toggle(i, interests, setInterests)}
            >
              {i}
            </button>
          ))}
        </div>
      </div>

      <div style={{ marginTop: 14 }}>
        <label>Dietary preferences</label>
        <div className="chips">
          {DIETARY.map((d) => (
            <button
              type="button"
              key={d}
              className="chip"
              aria-pressed={dietary.includes(d)}
              onClick={() => toggle(d, dietary, setDietary)}
            >
              {d.replace("_", " ")}
            </button>
          ))}
        </div>
      </div>

      <div
        className="row"
        style={{ marginTop: 18, justifyContent: "space-between" }}
      >
        <label className="row" style={{ marginBottom: 0 }}>
          <input
            type="checkbox"
            checked={direct}
            onChange={(e) => setDirect(e.target.checked)}
          />
          <span>Prefer direct flights</span>
        </label>
        <button className="primary" type="submit" disabled={!valid || busy}>
          {busy ? "Planning…" : "Plan my trip"}
        </button>
      </div>
    </form>
  );
}
