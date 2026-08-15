"""Langfuse instrumentation — inert without keys, and never fatal with them."""

from __future__ import annotations

import pytest

from app.services import langfuse as obs


@pytest.fixture(autouse=True)
def _clean_client():
    obs.reset_client()
    yield
    obs.reset_client()


# ---------------------------------------------------------------------------
# Unconfigured: everything is a no-op
# ---------------------------------------------------------------------------


def test_no_client_without_keys() -> None:
    assert obs.get_client() is None


def test_observe_is_a_no_op_without_keys() -> None:
    with obs.observe("thing") as span:
        assert span is None


def test_trace_is_a_no_op_without_keys() -> None:
    with obs.trace("plan-trip") as root:
        assert root is None


def test_helpers_are_silent_without_keys() -> None:
    """These are called on every request; they must not need a client."""
    obs.score("review_passed", 1.0)
    obs.update_current(output={"x": 1})
    assert obs.callbacks() == []
    assert obs.langchain_handler() is None


def test_exceptions_still_propagate_through_an_untraced_span() -> None:
    """Tracing must not swallow the workflow's own errors."""
    with pytest.raises(ValueError, match="boom"):
        with obs.observe("thing"):
            raise ValueError("boom")


# ---------------------------------------------------------------------------
# Configured: failures in tracing must not reach the workflow
# ---------------------------------------------------------------------------


class _ExplodingClient:
    """A Langfuse client where everything fails."""

    def start_as_current_observation(self, **_kwargs):
        raise RuntimeError("langfuse is down")

    def score_current_trace(self, **_kwargs):
        raise RuntimeError("langfuse is down")

    def update_current_span(self, **_kwargs):
        raise RuntimeError("langfuse is down")

    def flush(self):
        raise RuntimeError("langfuse is down")


def _use(client) -> None:
    obs._client = client
    obs._client_ready = True


def test_a_broken_tracer_does_not_break_the_request() -> None:
    """Observability that takes down what it observes is worse than none."""
    _use(_ExplodingClient())

    with obs.observe("thing") as span:
        assert span is None, "falls through untraced rather than raising"

    obs.score("review_passed", 1.0)
    obs.update_current(output={"x": 1})


def test_a_broken_tracer_still_lets_work_happen() -> None:
    _use(_ExplodingClient())
    ran = False
    with obs.observe("thing"):
        ran = True
    assert ran


# ---------------------------------------------------------------------------
# Configured and healthy: the right calls are made
# ---------------------------------------------------------------------------


class _Observation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingClient:
    def __init__(self) -> None:
        self.observations: list[dict] = []
        self.scores: list[dict] = []
        self.flushes = 0
        self.last = None

    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs)
        self.last = _Observation()
        return self.last

    def score_current_trace(self, **kwargs):
        self.scores.append(kwargs)

    def update_current_span(self, **kwargs):
        pass

    def flush(self):
        self.flushes += 1


def test_observation_records_name_and_type() -> None:
    client = _RecordingClient()
    _use(client)

    with obs.observe("flight-agent", as_type="agent", metadata={"k": "v"}):
        pass

    assert client.observations[0]["name"] == "flight-agent"
    assert client.observations[0]["as_type"] == "agent"
    assert client.observations[0]["metadata"] == {"k": "v"}


def test_a_failing_span_is_marked_as_an_error_and_still_raises() -> None:
    client = _RecordingClient()
    _use(client)

    with pytest.raises(ValueError):
        with obs.observe("flight-agent", as_type="agent"):
            raise ValueError("provider down")

    assert client.last is not None
    assert client.last.updates[0]["level"] == "ERROR"
    assert "provider down" in client.last.updates[0]["status_message"]


def test_trace_flushes_so_the_trace_appears_promptly() -> None:
    client = _RecordingClient()
    _use(client)

    with obs.trace("plan-trip", tags=["x"]):
        pass

    assert client.flushes == 1


def test_scores_reach_the_client() -> None:
    client = _RecordingClient()
    _use(client)

    obs.score("review_passed", 1.0, comment="all good")

    assert client.scores[0]["name"] == "review_passed"
    assert client.scores[0]["value"] == 1.0


# ---------------------------------------------------------------------------
# The graph and the API stay traced end to end
# ---------------------------------------------------------------------------


def test_every_graph_node_is_wrapped_in_an_observation(sample_request) -> None:
    """A trace that skips nodes is worse than none — it misleads."""
    from app.graph.graph import plan_trip

    client = _RecordingClient()
    _use(client)

    plan_trip(sample_request)

    names = {o["name"] for o in client.observations}
    expected = {
        "planner",
        "destination",
        "flight-agent",
        "hotel-agent",
        "activity-agent",
        "restaurant-agent",
        "transportation",
        "budget-agent",
        "itinerary-agent",
        "review-agent",
    }
    assert expected <= names, f"missing spans: {expected - names}"


def test_the_replan_loop_shows_up_as_repeated_spans(sample_request) -> None:
    """The repeats are the point: a loop you cannot see is a loop you
    cannot debug."""
    from app.graph.graph import plan_trip

    client = _RecordingClient()
    _use(client)

    plan_trip(sample_request)

    flight_spans = [o for o in client.observations if o["name"] == "flight-agent"]
    assert len(flight_spans) > 1, (
        "the budget loop re-ran flights; the trace must say so"
    )


def test_agents_and_tools_are_distinguishable() -> None:
    """projectIdea.md §15 wants agent reasoning separable from tool calls."""
    client = _RecordingClient()
    _use(client)

    with obs.observe("flight-agent", as_type="agent"):
        pass
    with obs.observe("amadeus/v2/shopping/flight-offers", as_type="tool"):
        pass

    types = {o["name"]: o["as_type"] for o in client.observations}
    assert types["flight-agent"] == "agent"
    assert types["amadeus/v2/shopping/flight-offers"] == "tool"
