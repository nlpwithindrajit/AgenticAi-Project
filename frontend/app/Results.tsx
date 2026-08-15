"use client";

import type {
  Activity,
  BudgetSummary,
  FlightOption,
  HotelOption,
  Restaurant,
  TripPlan,
} from "@/lib/types";

function money(value: number, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(value);
}

function hours(minutes: number | null | undefined): string {
  if (!minutes) return "";
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** Marks anything that did not come from a live provider search. */
function StubBadge({ source }: { source: string }) {
  if (source !== "stub") return null;
  return <span className="badge stub">stub</span>;
}

function BudgetPanel({ budget }: { budget: BudgetSummary }) {
  const used = Math.min(budget.estimated_total / budget.budget, 1);
  const rows: [string, number][] = [
    ["Flights", budget.breakdown.flights],
    ["Hotels", budget.breakdown.hotels],
    ["Activities", budget.breakdown.activities],
    ["Restaurants", budget.breakdown.restaurants],
    ["Transport", budget.breakdown.transportation],
  ];
  const max = Math.max(...rows.map(([, v]) => v), 1);

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>
          {money(budget.estimated_total, budget.currency)}{" "}
          <span className="muted small">
            of {money(budget.budget, budget.currency)}
          </span>
        </strong>
        <span className={budget.over_budget ? "muted" : "muted"}>
          {budget.over_budget
            ? `over by ${money(-budget.remaining, budget.currency)}`
            : `${money(budget.remaining, budget.currency)} left`}
        </span>
      </div>
      <div className={`meter${budget.over_budget ? " over" : ""}`}>
        <span style={{ width: `${used * 100}%` }} />
      </div>
      <div className="bars">
        {rows.map(([label, value]) => (
          <div className="bar-row" key={label}>
            <span className="muted">{label}</span>
            <span
              className="bar"
              style={{ width: `${Math.max((value / max) * 100, 1)}%` }}
            />
            <span className="num">{money(value, budget.currency)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function FlightCard({ flight, top }: { flight: FlightOption; top: boolean }) {
  return (
    <div className={`option${top ? " top" : ""}`}>
      <div className="head">
        <span className="name">{flight.airline_name ?? flight.airline}</span>
        {top && <span className="badge best">best match</span>}
        <StubBadge source={flight.source} />
        <span className="price">{money(flight.price, flight.currency)}</span>
      </div>
      <div className="small muted">
        {flight.outbound.origin} → {flight.outbound.destination}
        {flight.inbound ? " · return included" : " · one way"} ·{" "}
        {flight.stops === 0 ? "non-stop" : `${flight.stops} stop`} ·{" "}
        {hours(flight.total_duration_minutes)} · score {flight.score}
      </div>
      {flight.rationale && <p className="why">{flight.rationale}</p>}
    </div>
  );
}

function HotelCard({ hotel, top }: { hotel: HotelOption; top: boolean }) {
  return (
    <div className={`option${top ? " top" : ""}`}>
      <div className="head">
        <span className="name">{hotel.name}</span>
        {top && <span className="badge best">best in {hotel.destination}</span>}
        <StubBadge source={hotel.source} />
        <span className="price">{money(hotel.total_price, hotel.currency)}</span>
      </div>
      <div className="small muted">
        {hotel.destination} · {hotel.nights} night
        {hotel.nights === 1 ? "" : "s"} ·{" "}
        {money(hotel.price_per_night, hotel.currency)}/night
        {hotel.distance_km != null && ` · ${hotel.distance_km} km from centre`}
        {hotel.rating != null
          ? ` · guest rating ${hotel.rating}/100`
          : " · no guest rating"}{" "}
        · score {hotel.score}
      </div>
      {hotel.rationale && <p className="why">{hotel.rationale}</p>}
    </div>
  );
}

function ActivityRow({ activity }: { activity: Activity }) {
  return (
    <div className="option">
      <div className="head">
        <span className="name">{activity.activity}</span>
        <StubBadge source={activity.source} />
        {activity.cost_is_estimated && <span className="badge est">no price</span>}
        <span className="price">
          {activity.estimated_cost > 0
            ? money(activity.estimated_cost, activity.currency)
            : "—"}
        </span>
      </div>
      <div className="small muted">
        Day {activity.recommended_day} · {activity.destination}
        {activity.rating != null && ` · rated ${activity.rating}/5`}
      </div>
    </div>
  );
}

function RestaurantRow({ restaurant }: { restaurant: Restaurant }) {
  return (
    <div className="option">
      <div className="head">
        <span className="name">{restaurant.name}</span>
        <StubBadge source={restaurant.source} />
        {restaurant.price_is_estimated && (
          <span className="badge est" title={restaurant.estimate_basis ?? ""}>
            estimated
          </span>
        )}
        <span className="price">
          ~{money(restaurant.price_estimate, restaurant.currency)}
        </span>
      </div>
      <div className="small muted">
        Day {restaurant.recommended_day} · {restaurant.destination}
        {restaurant.cuisine && ` · ${restaurant.cuisine}`}
      </div>
    </div>
  );
}

export default function Results({ plan }: { plan: TripPlan }) {
  const review = plan.review;
  const bestHotelPerCity = new Map<string, string>();
  for (const hotel of plan.hotel_recommendations) {
    const current = bestHotelPerCity.get(hotel.destination);
    const currentScore = plan.hotel_recommendations.find(
      (h) => h.name === current,
    )?.score;
    if (current === undefined || hotel.score > (currentScore ?? -1)) {
      bestHotelPerCity.set(hotel.destination, hotel.name);
    }
  }

  return (
    <section>
      <div className="row" style={{ gap: 12, marginBottom: 4 }}>
        {review && (
          <span
            className={`verdict ${review.verdict === "PASS" ? "pass" : "fail"}`}
          >
            Review {review.verdict}
          </span>
        )}
        {plan.trace_id && (
          <span className="muted small">trace {plan.trace_id.slice(0, 8)}</span>
        )}
      </div>

      {review?.verdict === "FAIL" && review.issues.length > 0 && (
        <div className="error" style={{ marginTop: 12 }}>
          <strong>This plan did not pass review.</strong>
          <ul className="notes" style={{ marginTop: 6, color: "inherit" }}>
            {review.issues.map((issue, i) => (
              <li key={i}>
                {issue.severity === "error" ? "✗" : "!"} {issue.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      {plan.budget && (
        <>
          <h2>Budget</h2>
          <BudgetPanel budget={plan.budget} />
        </>
      )}

      {plan.flight_recommendations.length > 0 && (
        <>
          <h2>Flights</h2>
          {plan.flight_recommendations.map((f, i) => (
            <FlightCard key={f.offer_id ?? i} flight={f} top={i === 0} />
          ))}
          <p className="small muted">
            These are alternatives — only the best match is counted in the
            budget.
          </p>
        </>
      )}

      {plan.hotel_recommendations.length > 0 && (
        <>
          <h2>Hotels</h2>
          {plan.hotel_recommendations.map((h, i) => (
            <HotelCard
              key={h.hotel_id ?? i}
              hotel={h}
              top={bestHotelPerCity.get(h.destination) === h.name}
            />
          ))}
          <p className="small muted">
            Alternatives per city — the budget counts the best in each.
          </p>
        </>
      )}

      {plan.daily_itinerary.length > 0 && (
        <>
          <h2>Itinerary</h2>
          <div className="card">
            {plan.daily_itinerary.map((day) => (
              <div className="day" key={day.day}>
                <h3>
                  Day {day.day} — {day.destination}
                  <span className="date">{day.date}</span>
                </h3>
                {day.items.map((item, i) => (
                  <div className="slot" key={i}>
                    <time>{item.time}</time>
                    <span>{item.title}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </>
      )}

      {plan.activities.length > 0 && (
        <>
          <h2>Activities</h2>
          {plan.activities.map((a, i) => (
            <ActivityRow key={i} activity={a} />
          ))}
        </>
      )}

      {plan.restaurants.length > 0 && (
        <>
          <h2>Restaurants</h2>
          {plan.restaurants.map((r, i) => (
            <RestaurantRow key={i} restaurant={r} />
          ))}
          <p className="small muted">
            Venues come from a places search; the prices are estimates, not
            quotes.
          </p>
        </>
      )}

      {plan.errors.length > 0 && (
        <>
          <h2>What this plan says about itself</h2>
          <div className="card">
            <ul className="notes">
              {plan.errors.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          </div>
        </>
      )}
    </section>
  );
}
