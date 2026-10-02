from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from quantro.domain.trading import Fill, OrderState, Side


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    buying_power: Decimal
    as_of: datetime
    includes_local_reservations: bool = False


@dataclass(frozen=True)
class OrderRequest:
    order_id: str
    client_order_id: str
    account_id: str
    instrument_id: str
    side: Side
    quantity: int
    limit_price: Decimal
    fee_rate: Decimal
    sent_at: datetime
    tif: str = "DAY"


@dataclass(frozen=True)
class SubmitResult:
    status: str
    broker_order_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    state: OrderState
    filled_quantity: int
    fills: tuple[Fill, ...]
    broker_order_id: str | None = None


class BrokerPort(Protocol):
    broker_id: str
    execution_mode: str

    def ready(self) -> bool: ...
    def submit(self, request: OrderRequest) -> SubmitResult: ...
    def cancel(self, order_id: str) -> None: ...
    def query_order(self, order_id: str) -> OrderSnapshot: ...
