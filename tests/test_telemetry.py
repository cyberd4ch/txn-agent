"""Tracing is optional and must never break a purchase.

These tests use an injected tracer rather than a real exporter: the point is to pin
the contract (span names, the facts recorded, and the no-op path when
`opentelemetry-api` is absent), not to test somebody else's SDK.
"""
import importlib.util
from decimal import Decimal as D

import pytest

from txn_agent import telemetry
from txn_agent.agent import TransactionalAgent
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request
from txn_agent.llm import run_llm_request
from txn_agent.models import Cart, CartItem
from txn_agent.payments import DemoVault
from txn_agent.tools import ToolRouter


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, dict]] = []
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append((name, attributes or {}))

    def record_exception(self, exception: BaseException) -> None:
        self.exceptions.append(exception)


class FakeContext:
    def __init__(self, span: FakeSpan):
        self.span = span

    def __enter__(self) -> FakeSpan:
        return self.span

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str, **kwargs: object) -> FakeContext:
        span = FakeSpan(name)
        self.spans.append(span)
        return FakeContext(span)

    def named(self, name: str) -> FakeSpan:
        matches = [s for s in self.spans if s.name == name]
        assert matches, f"no span named {name!r}; got {[s.name for s in self.spans]}"
        return matches[-1]


@pytest.fixture(autouse=True)
def _fresh_tracer():
    telemetry.reset()
    yield
    telemetry.reset()


def make_agent(confirm=None) -> TransactionalAgent:
    return TransactionalAgent(default_connectors(), DemoVault(), confirm=confirm)


def make_cart(agent: TransactionalAgent, query: str = "dishwasher lower rack wheel kit under $50") -> Cart:
    """A one-line cart built from a real search, so the cart path is exercised too."""
    offers = [o for o in agent.search(parse_request(query)) if o.in_stock]
    assert offers, "fixture search returned nothing in stock"
    return Cart(items=(CartItem(offer=offers[0]),), cart_id="test-cart")


def test_configured_tracer_is_used() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)
    assert telemetry._tracer() is tracer


def test_checkout_records_gate_decision_outcome_and_key() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)

    agent = make_agent()
    out = agent.checkout_cart(make_cart(agent))
    assert out.status.value == "purchased"

    span = tracer.named(telemetry.SPAN_CHECKOUT)
    assert span.attributes[telemetry.ATTR_GATE_DECISION] == "auto_buy"
    assert span.attributes[telemetry.ATTR_OUTCOME] == "purchased"
    assert span.attributes[telemetry.ATTR_MERCHANT] == "AppliancePartsCo"
    # The key is a hash, not a cart dump: safe to put on a span.
    assert len(str(span.attributes[telemetry.ATTR_IDEMPOTENCY_KEY])) == 32
    # Decimals are stringified so the attribute type stays valid.
    assert isinstance(span.attributes[telemetry.ATTR_CART_TOTAL], str)
    assert telemetry.ATTR_APPROVAL_REQUIRED not in span.attributes


def test_declined_approval_is_a_child_span_with_the_verdict() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)

    # Flights always need approval; no approver wired means fail closed -> declined.
    # This exercises the single-offer path (`run`), which has its own span.
    out = make_agent().run(parse_request("flight SFO to JFK under $400"))
    assert out.status.value == "declined"

    checkout = tracer.named(telemetry.SPAN_PURCHASE)
    assert checkout.attributes[telemetry.ATTR_GATE_DECISION] == "confirm"
    assert checkout.attributes[telemetry.ATTR_APPROVAL_REQUIRED] is True
    assert checkout.attributes[telemetry.ATTR_OUTCOME] == "declined"

    approval = tracer.named(telemetry.SPAN_APPROVAL)
    assert approval.attributes[telemetry.ATTR_APPROVED] is False


def test_tool_spans_carry_genai_attributes_and_no_arguments() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)

    router = ToolRouter(make_agent(), budget_ceiling=D("150"))
    out = router.call("search_offers",
                      {"vertical": "parts", "query": "door gasket", "max_total": 999})
    assert "offers" in out

    span = tracer.named("execute_tool search_offers")
    assert span.attributes[telemetry.ATTR_TOOL_NAME] == "search_offers"
    assert span.attributes[telemetry.ATTR_OPERATION] == "execute_tool"
    assert span.attributes[telemetry.ATTR_VERTICAL] == "parts"
    # The model's prose never lands in telemetry.
    assert "door gasket" not in str(span.attributes.values())


def test_tool_errors_are_marked_not_raised() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)

    router = ToolRouter(make_agent(), budget_ceiling=D("150"))
    out = router.call("search_offers", {})  # malformed model arguments
    assert "error" in out

    span = tracer.named("execute_tool search_offers")
    assert span.attributes[telemetry.ATTR_TOOL_ERROR] == "invalid_arguments"


class FakeBlock:
    def __init__(self, block: dict) -> None:
        self.__dict__.update(block)


class FakeUsage:
    input_tokens = 120
    output_tokens = 30


class FakeResponse:
    stop_reason = "end_turn"
    role = "assistant"
    usage = FakeUsage()

    def __init__(self) -> None:
        self.content = [FakeBlock({"type": "text", "text": "Nothing needed."})]


class FakeClient:
    def __init__(self) -> None:
        self.messages = self

    def create(self, **kwargs: object) -> FakeResponse:
        return FakeResponse()


def test_llm_run_records_agent_chat_and_usage() -> None:
    tracer = FakeTracer()
    telemetry.configure(tracer)

    result = run_llm_request(make_agent(), "check the wheel kit", budget_ceiling=D("150"),
                             model="test-model", client=FakeClient())
    assert result.stopped_reason == "end_turn"

    run = tracer.named(telemetry.SPAN_AGENT_RUN)
    assert run.attributes[telemetry.ATTR_OPERATION] == "invoke_agent"
    assert run.attributes[telemetry.ATTR_MODEL] == "test-model"
    assert run.attributes[telemetry.ATTR_TURNS] == 1
    assert run.attributes[telemetry.ATTR_STOP_REASON] == "end_turn"

    chat = tracer.named("chat test-model")
    assert chat.attributes[telemetry.ATTR_INPUT_TOKENS] == 120
    assert chat.attributes[telemetry.ATTR_OUTPUT_TOKENS] == 30


@pytest.mark.skipif(importlib.util.find_spec("opentelemetry") is not None,
                    reason="opentelemetry-api is installed; the no-op path is not the default")
def test_span_is_a_noop_without_opentelemetry() -> None:
    with telemetry.span("anything", {"a": 1}) as active:
        active.set_attribute("b", 2)
        active.add_event("e")
        telemetry.set_attribute(active, "c", None)  # None attributes are dropped
