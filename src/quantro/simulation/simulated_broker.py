from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from quantro.domain.models import Conflict, DomainError
from quantro.domain.trading import Fill, MarketBar, OrderState, Side
from quantro.ports.broker import OrderRequest, OrderSnapshot, SubmitResult


@dataclass
class SimOrder:
    request: OrderRequest
    state: OrderState = OrderState.ACKNOWLEDGED
    fills: list[Fill] = field(default_factory=list)


class SimulatedBroker:
    broker_id = "simulated"

    def __init__(self, *, execution_mode: str = "BACKTEST", participation: Decimal,
                 slippage_rate: Decimal, sell_tax_rate: Decimal, settlement_days: int = 0,
                 lot_sizes: dict[str, int] | None = None):
        if execution_mode not in {"BACKTEST", "PAPER"}:
            raise DomainError("Simulated broker cannot execute LIVE")
        for rate in (participation, slippage_rate, sell_tax_rate):
            if not isinstance(rate, Decimal) or not rate.is_finite() or not 0 <= rate <= 1:
                raise DomainError("Simulation rates must be Decimal values in [0, 1]")
        if participation == 0 or type(settlement_days) is not int or settlement_days < 0:
            raise DomainError("Invalid participation/settlement model")
        self.execution_mode = execution_mode
        self.participation, self.slippage_rate = participation, slippage_rate
        self.sell_tax_rate, self.settlement_days = sell_tax_rate, settlement_days
        self.lot_sizes = dict(lot_sizes or {})
        if any(type(lot) is not int or lot <= 0 for lot in self.lot_sizes.values()):
            raise DomainError("Invalid lot size")
        self.orders: dict[str, SimOrder] = {}
        self._clients: dict[str, str] = {}

    def ready(self) -> bool:
        return True

    def submit(self, request: OrderRequest) -> SubmitResult:
        if request.client_order_id in self._clients:
            old = self.orders[self._clients[request.client_order_id]]
            if old.request != request:
                raise Conflict("Client order id reused with a different order")
            return SubmitResult("ACCEPTED", "sim:" + old.request.order_id)
        if request.order_id in self.orders:
            raise Conflict("Order id already exists")
        self.orders[request.order_id] = SimOrder(request)
        self._clients[request.client_order_id] = request.order_id
        return SubmitResult("ACCEPTED", "sim:" + request.order_id)

    def cancel(self, order_id: str) -> None:
        order = self.orders[order_id]
        if order.state not in {OrderState.FILLED, OrderState.EXPIRED, OrderState.CANCELED}:
            order.state = OrderState.CANCELED

    def query_order(self, order_id: str) -> OrderSnapshot:
        order = self.orders.get(order_id)
        if order is None:
            return OrderSnapshot(order_id, OrderState.UNKNOWN, 0, ())
        return OrderSnapshot(order_id, order.state, sum(f.quantity for f in order.fills),
                             tuple(order.fills), "sim:" + order_id)

    def on_bar(self, bar: MarketBar) -> tuple[OrderSnapshot, ...]:
        """Next-bar raw-price DAY approximation; no intrabar queue/path model."""
        remaining_volume = int(Decimal(bar.volume) * self.participation)
        snapshots = []
        for order in sorted(self.orders.values(), key=lambda o: (o.request.sent_at, o.request.client_order_id)):
            request = order.request
            if (request.instrument_id != bar.instrument_id or request.sent_at >= bar.start_at
                    or order.state not in {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}):
                continue
            price = None
            if bar.trading and bar.volume:
                if request.side == Side.BUY:
                    if bar.open <= request.limit_price:
                        price = bar.open
                    elif bar.low <= request.limit_price:
                        price = request.limit_price
                    if price is not None:
                        price = (price * (1 + self.slippage_rate)).to_integral_value(rounding=ROUND_CEILING)
                        if price > request.limit_price:
                            price = None
                else:
                    if bar.open >= request.limit_price:
                        price = bar.open
                    elif bar.high >= request.limit_price:
                        price = request.limit_price
                    if price is not None:
                        price = (price * (1 - self.slippage_rate)).to_integral_value(rounding=ROUND_FLOOR)
                        if price < request.limit_price or price <= 0:
                            price = None
            lot = self.lot_sizes.get(request.instrument_id, 1)
            remaining = request.quantity - sum(f.quantity for f in order.fills)
            quantity = min(remaining, remaining_volume // lot * lot) if price is not None else 0
            if quantity:
                fee = (quantity * price * request.fee_rate).to_integral_value(rounding=ROUND_CEILING)
                tax = ((quantity * price * self.sell_tax_rate).to_integral_value(rounding=ROUND_CEILING)
                       if request.side == Side.SELL else Decimal(0))
                fill = Fill(self.broker_id, f"{request.client_order_id}:{bar.id}", request.order_id,
                            quantity, price, fee, tax, bar.end_at,
                            bar.end_at + timedelta(days=self.settlement_days))
                order.fills.append(fill)
                remaining_volume -= quantity
            order.state = OrderState.FILLED if quantity == remaining else OrderState.EXPIRED
            snapshots.append(self.query_order(request.order_id))
        return tuple(snapshots)
