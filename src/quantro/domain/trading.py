from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .models import DomainError, State, aware, money
from .sizing import validate_ratio


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(str, Enum):
    RESERVED = "RESERVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.REJECTED}
TRANSITIONS = {
    OrderState.RESERVED: {OrderState.SUBMITTING, OrderState.CANCELED},
    OrderState.SUBMITTING: {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.UNKNOWN,
                            OrderState.PARTIALLY_FILLED, OrderState.FILLED},
    OrderState.UNKNOWN: {OrderState.ACKNOWLEDGED, OrderState.REJECTED,
                        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                             OrderState.CANCEL_PENDING, OrderState.EXPIRED, OrderState.CANCELED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                 OrderState.CANCEL_PENDING, OrderState.EXPIRED, OrderState.CANCELED},
    OrderState.CANCEL_PENDING: {OrderState.CANCELED, OrderState.PARTIALLY_FILLED,
                               OrderState.FILLED, OrderState.EXPIRED},
}


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    allocation_id: str
    strategy_assignment_id: str
    side: Side
    sizing_method: str
    ratio: Decimal
    evaluation_id: str
    reason: str

    def __post_init__(self):
        validate_ratio(self.ratio)
        if self.side not in {Side.BUY, Side.SELL}:
            raise DomainError("Unsupported side")
        expected = "BUDGET_PERCENT" if self.side == Side.BUY else "POSITION_PERCENT"
        if self.sizing_method != expected:
            raise DomainError("Unsupported sizing method for side")


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    as_of: datetime
    trading: bool = True
    session_open: bool = True

    def __post_init__(self):
        money(self.bid, positive=True)
        money(self.ask, positive=True)
        aware(self.as_of)
        if self.bid > self.ask:
            raise DomainError("Bid exceeds ask")


@dataclass(frozen=True)
class MarketBar:
    id: str
    instrument_id: str
    start_at: datetime
    end_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trading: bool = True

    def __post_init__(self):
        for time in (self.start_at, self.end_at, self.available_at):
            aware(time)
        for price in (self.open, self.high, self.low, self.close):
            money(price, positive=True)
        if (self.start_at >= self.end_at or self.available_at < self.end_at
                or self.low > min(self.open, self.close) or self.high < max(self.open, self.close)
                or self.low > self.high or type(self.volume) is not int or self.volume < 0):
            raise DomainError("Invalid daily market bar")


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    max_order_amount: Decimal
    max_daily_amount: Decimal
    max_quote_age_seconds: int
    fee_rate: Decimal

    def __post_init__(self):
        money(self.max_order_amount, positive=True)
        money(self.max_daily_amount, positive=True)
        if not self.version or type(self.max_quote_age_seconds) is not int or self.max_quote_age_seconds < 0:
            raise DomainError("Risk policy requires a version and quote-age limit")
        if not isinstance(self.fee_rate, Decimal) or not self.fee_rate.is_finite() or not 0 <= self.fee_rate <= 1:
            raise DomainError("Fee upper-bound rate must be in [0, 1]")

    def fee_upper_bound(self, quantity: int, price: Decimal) -> Decimal:
        from decimal import ROUND_CEILING
        # A per-share ceiling also covers multiple independently rounded partial fills.
        return quantity * (price * self.fee_rate).to_integral_value(rounding=ROUND_CEILING)


@dataclass
class Order:
    id: str
    client_order_id: str
    allocation_id: str
    account_id: str
    instrument_id: str
    evaluation_id: str
    side: Side
    quantity: int
    limit_price: Decimal
    target_amount: Decimal
    reserved_cash: Decimal
    reserved_quantity: int
    fee_rate: Decimal
    created_at: datetime
    state: OrderState = OrderState.RESERVED
    filled_quantity: int = 0
    broker_order_id: str | None = None
    revision: int = 0

    def transition(self, state: OrderState) -> None:
        if state not in TRANSITIONS.get(self.state, set()):
            raise DomainError(f"Invalid order transition: {self.state} -> {state}")
        self.state = state
        self.revision += 1

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True)
class Fill:
    broker_id: str
    execution_id: str
    order_id: str
    quantity: int
    price: Decimal
    fee: Decimal
    tax: Decimal
    trade_at: datetime
    settlement_at: datetime

    def __post_init__(self):
        if not self.broker_id or not self.execution_id or type(self.quantity) is not int or self.quantity <= 0:
            raise DomainError("Fill requires broker execution identity and positive quantity")
        money(self.price, positive=True)
        money(self.fee)
        money(self.tax)
        aware(self.trade_at)
        aware(self.settlement_at)
        if self.settlement_at < self.trade_at:
            raise DomainError("Settlement precedes trade")


@dataclass
class FillRecord:
    fill: Fill
    transaction_id: str
    settled: bool


@dataclass
class OutboxEvent:
    id: str
    order_id: str
    created_at: datetime
    dispatched: bool = False


@dataclass(frozen=True)
class Evaluation:
    id: str
    allocation_id: str
    market_event_id: str
    evaluated_at: datetime
    assignment_revisions: tuple[tuple[str, int], ...]
    context_hash: str
    status: str
    reason: str
    selected: OrderIntent | None = None
    suppressed: tuple[OrderIntent, ...] = ()
    order_id: str | None = None
    policy_version: str | None = None


def validate_trading_state(state: State) -> None:
    cash = {key: Decimal(0) for key in state.allocations}
    quantity = dict.fromkeys(state.allocations, 0)
    clients = set()
    for order in state.orders.values():
        if order.client_order_id in clients:
            raise DomainError("Duplicate client order id")
        clients.add(order.client_order_id)
        allocation = state.allocations[order.allocation_id]
        instrument = state.instruments[order.instrument_id]
        if allocation.account_id != order.account_id or allocation.instrument_id != order.instrument_id:
            raise DomainError("Order crosses allocation/account/instrument scope")
        if (type(order.quantity) is not int or order.quantity <= 0 or order.quantity % instrument.lot_size
                or not 0 <= order.filled_quantity <= order.quantity or order.reserved_quantity < 0):
            raise DomainError("Invalid order quantity")
        money(order.limit_price, positive=True)
        money(order.reserved_cash)
        if order.state in TERMINAL and (order.reserved_cash or order.reserved_quantity):
            raise DomainError("Terminal order retains reservations")
        cash[allocation.id] += order.reserved_cash
        quantity[allocation.id] += order.reserved_quantity
    for allocation_id in cash:
        if (state.allocations[allocation_id].cash_reserved != cash[allocation_id]
                or state.allocations[allocation_id].quantity_reserved != quantity[allocation_id]):
            raise DomainError("Allocation reservations disagree with orders")
    executed = dict.fromkeys(state.orders, 0)
    for key, record in state.fills.items():
        fill = record.fill
        if key != (fill.broker_id, fill.execution_id) or fill.order_id not in state.orders:
            raise DomainError("Invalid persisted fill identity")
        executed[fill.order_id] += fill.quantity
    for order_id, count in executed.items():
        if count != state.orders[order_id].filled_quantity:
            raise DomainError("Order cumulative quantity disagrees with recorded fills")
    priorities = set()
    for assignment in state.assignments.values():
        if assignment.enabled and not assignment.archived:
            key = (assignment.allocation_id, assignment.priority)
            if key in priorities:
                raise DomainError("Active strategy priorities must be distinct")
            priorities.add(key)
