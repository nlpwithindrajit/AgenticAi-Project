/**
 * Mirrors the FastAPI schema in `app/models/travel.py`.
 *
 * The fields that say how trustworthy a number is — `source`, `cost_is_estimated`,
 * `price_is_estimated`, `estimate_basis` — are deliberately part of this type.
 * The backend is careful to distinguish a quoted price from an estimate and a
 * real search from stub inventory; a UI that dropped those distinctions would
 * quietly undo that work.
 */

export type TripStyle = "relaxed" | "balanced" | "packed";
export type Source = "amadeus" | "stub";

export interface TravelRequest {
  origin: string;
  destinations: string[];
  departure_date: string;
  return_date: string;
  travelers: number;
  budget: number;
  currency: string;
  hotel_stars?: number | null;
  preferred_airline?: string | null;
  direct_flights_only: boolean;
  interests: string[];
  dietary_preferences: string[];
  trip_style: TripStyle;
}

export interface FlightSegment {
  carrier_code: string;
  carrier_name?: string | null;
  flight_number?: string | null;
  origin: string;
  destination: string;
  departure_at: string;
  arrival_at: string;
  duration_minutes?: number | null;
}

export interface FlightSlice {
  origin: string;
  destination: string;
  departure_at: string;
  arrival_at: string;
  duration_minutes?: number | null;
  segments: FlightSegment[];
  stops: number;
}

export interface FlightOption {
  offer_id?: string | null;
  airline: string;
  airline_name?: string | null;
  outbound: FlightSlice;
  inbound?: FlightSlice | null;
  price: number;
  price_per_traveler?: number | null;
  currency: string;
  score: number;
  rationale?: string | null;
  source: Source;
  stops: number;
  total_duration_minutes: number;
}

export interface HotelOption {
  hotel_id?: string | null;
  name: string;
  destination: string;
  check_in: string;
  check_out: string;
  nights: number;
  price_per_night: number;
  total_price: number;
  currency: string;
  distance_km?: number | null;
  stars?: number | null;
  rating?: number | null;
  room_type?: string | null;
  amenities: string[];
  score: number;
  score_components: Record<string, number>;
  rationale?: string | null;
  source: Source;
}

export interface Activity {
  activity: string;
  category: string;
  destination: string;
  description?: string | null;
  duration_hours?: number | null;
  estimated_cost: number;
  cost_is_estimated: boolean;
  currency: string;
  rating?: number | null;
  booking_link?: string | null;
  recommended_day?: number | null;
  score: number;
  rationale?: string | null;
  source: Source;
}

export interface Restaurant {
  name: string;
  destination: string;
  cuisine?: string | null;
  meal: string;
  price_estimate: number;
  price_is_estimated: boolean;
  estimate_basis?: string | null;
  currency: string;
  dietary_tags: string[];
  recommended_day?: number | null;
  score: number;
  rationale?: string | null;
  source: Source;
}

export interface ItineraryItem {
  time: string;
  title: string;
  kind: string;
  location?: string | null;
  notes?: string | null;
}

export interface DayPlan {
  day: number;
  date: string;
  destination: string;
  items: ItineraryItem[];
}

export interface BudgetBreakdown {
  flights: number;
  hotels: number;
  activities: number;
  restaurants: number;
  transportation: number;
  currency: string;
  estimated_total: number;
}

export interface BudgetSummary {
  breakdown: BudgetBreakdown;
  estimated_total: number;
  budget: number;
  remaining: number;
  over_budget: boolean;
  currency: string;
}

export interface ReviewIssue {
  severity: "error" | "warning";
  check: string;
  detail: string;
}

export interface ReviewResult {
  verdict: "PASS" | "FAIL";
  issues: ReviewIssue[];
}

export interface TransportLeg {
  day: number;
  from_location: string;
  to_location: string;
  mode: string;
  duration_minutes?: number | null;
  estimated_cost: number;
  currency: string;
}

export interface TripPlan {
  request: TravelRequest;
  flight_recommendations: FlightOption[];
  hotel_recommendations: HotelOption[];
  activities: Activity[];
  restaurants: Restaurant[];
  transportation_plan: TransportLeg[];
  daily_itinerary: DayPlan[];
  budget?: BudgetSummary | null;
  review?: ReviewResult | null;
  /** Notes the plan makes about itself: stub fallbacks, loop iterations, limits. */
  errors: string[];
  trace_id?: string | null;
}
