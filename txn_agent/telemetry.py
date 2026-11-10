"""Optional OpenTelemetry tracing for agent runs.

The core keeps its stdlib-only promise: OpenTelemetry is never imported at module
load. With ``opentelemetry-api`` installed (``pip install 'txn-agent[otel]'``) and a
provider configured by the host, spans go wherever that provider sends them; without
it every helper here is a no-op, so importing and running the package is unchanged.

``configure()`` also accepts any tracer-shaped object, which is how the tests assert
on spans without pulling a real exporter into CI.

Naming follows the OpenTelemetry GenAI conventions where they exist (agent
invocation, chat, tool execution) and a ``txn_agent.*`` namespace for the domain
facts this package is actually about: gate decisions, budgets, idempotency keys.
Semantic conventions are still moving, so every attribute name lives in this file —
a rename is one edit, not a grep.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

INSTRUMENTATION_NAME = "txn-agent"

# Span names. GenAI conventions: "{operation} {name}" for agent/chat/tool spans.
SPAN_PURCHASE = "txn_agent.purchase"
SPAN_CHECKOUT = "txn_agent.checkout_cart"
SPAN_APPROVAL = "txn_agent.approval"
SPAN_AGENT_RUN = "invoke_agent txn-agent"
SPAN_CHAT = "chat {}"
SPAN_TOOL = "execute_tool {}"

# --- OpenTelemetry GenAI semantic convention keys -------------------------------
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_PROVIDER = "gen_ai.provider.name"
ATTR_MODEL = "gen_ai.request.model"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# --- domain keys ----------------------------------------------------------------
ATTR_CART_ID = "txn_agent.cart.id"
ATTR_CART_TOTAL = "txn_agent.cart.total"
ATTR_CART_LINES = "txn_agent.cart.lines"
ATTR_MERCHANT = "txn_agent.merchant"
ATTR_VERTICAL = "txn_agent.vertical"
ATTR_GATE_DECISION = "txn_agent.gate.decision"
ATTR_GATE_REASONS = "txn_agent.gate.reasons"
ATTR_OUTCOME = "txn_agent.outcome"
ATTR_IDEMPOTENCY_KEY = "txn_agent.idempotency_key"
ATTR_APPROVAL_REQUIRED = "txn_agent.approval.required"
ATTR_APPROVED = "txn_agent.approval.approved"
ATTR_TOOL_ERROR = "txn_agent.tool.error"
ATTR_TURNS = "txn_agent.llm.turns"
ATTR_STOP_REASON = "txn_agent.llm.stop_reason"

_ABSENT = object()


def _coerce(value: Any) -> Any:
    """Map Python values onto the primitive types an attribute may hold."""
    from decimal import Decimal

    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if value is None:
        return _ABSENT
    return str(value)


class _NullSpan:
    """Span-shaped no-op, so call sites never need a branch."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102 - no-op
        return None

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        return None

    def record_exception(self, exception: BaseException) -> None:
        return None


class _NullContext:
    def __enter__(self) -> _NullSpan:
        return _NullSpan()

    def __exit__(self, *exc: object) -> Literal[False]:
        return False


class _NullTracer:
    def start_as_current_span(self, name: str, **kwargs: Any) -> _NullContext:
        return _NullContext()


_TRACER: Any = _ABSENT


def _tracer() -> Any:
    global _TRACER
    if _TRACER is not _ABSENT:
        return _TRACER
    try:
        from opentelemetry import trace  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        _TRACER = _NullTracer()
    else:
        _TRACER = trace.get_tracer(INSTRUMENTATION_NAME)
    return _TRACER


def configure(tracer: object) -> None:
    """Install a tracer (a real one, or a test double). `None` restores detection."""
    global _TRACER
    _TRACER = _ABSENT if tracer is None else tracer


def reset() -> None:
    """Forget any injected tracer and re-detect OpenTelemetry on next use."""
    configure(None)


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Open a span, set attributes as primitives, and never let tracing raise.

    Observability must not be able to break a purchase: a tracer that throws is
    swallowed here rather than propagated into the transaction path.
    """
    try:
        ctx = _tracer().start_as_current_span(name)
    except Exception:  # pragma: no cover - defensive: a broken tracer is not fatal
        yield _NullSpan()
        return
    with _ctx(ctx) as active:
        if attributes:
            set_attributes(active, attributes)
        yield active


class _ctx:  # noqa: N801 - tiny helper, private
    """Enter whatever the tracer returned, falling back to a no-op span."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._entered: Any = None

    def __enter__(self) -> Any:
        try:
            self._entered = self._ctx.__enter__()
        except Exception:  # pragma: no cover
            self._entered = _NullSpan()
        return self._entered if self._entered is not None else _NullSpan()

    def __exit__(self, *exc: object) -> Literal[False]:
        # Never report suppression: a span must not swallow the caller's exception.
        try:
            self._ctx.__exit__(*exc)
        except Exception:  # pragma: no cover
            pass
        return False


def set_attributes(active: Any, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        coerced = _coerce(value)
        if coerced is _ABSENT:
            continue
        try:
            active.set_attribute(key, coerced)
        except Exception:  # pragma: no cover
            continue


def set_attribute(active: Any, key: str, value: Any) -> None:
    set_attributes(active, {key: value})


def add_event(active: Any, name: str, attributes: Mapping[str, Any] | None = None) -> None:
    try:
        active.add_event(name, attributes or {})
    except Exception:  # pragma: no cover
        return None


def record_exception(active: Any, exc: BaseException) -> None:
    try:
        active.record_exception(exc)
    except Exception:  # pragma: no cover
        return None


def tool_span_name(tool: str) -> str:
    return SPAN_TOOL.format(tool)


def chat_span_name(model: str) -> str:
    return SPAN_CHAT.format(model)
