from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


class DomainError(ValueError):
    pass


class Conflict(DomainError):
    pass


def money(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainError("Money must be a finite Decimal")
    if value < 0 or (positive and value == 0) or value != value.to_integral_value():
        raise DomainError("KRW must be nonnegative whole won (positive for transfers)")
    return value


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("Timestamp must include a timezone")
    return value


@dataclass(frozen=True)
class Instrument:
    id: str
    symbol: str
    exchange: str
    currency: str = "KRW"
    lot_size: int = 1

    def __post_init__(self) -> None:
        if self.currency != "KRW" or type(self.lot_size) is not int or self.lot_size <= 0:
            raise DomainError("Only KRW instruments with positive integer lots are supported")


@dataclass
class Allocation:
    id: str
    account_id: str
    instrument_id: str
    name: str
    principal: Decimal = Decimal(0)
    cash_total: Decimal = Decimal(0)
    cash_reserved: Decimal = Decimal(0)
    cash_unsettled: Decimal = Decimal(0)
    quantity: int = 0
    quantity_reserved: int = 0
    position_cost: Decimal = Decimal(0)
    revision: int = 0

    @property
    def cash_available(self) -> Decimal:
        return self.cash_total - self.cash_reserved - self.cash_unsettled

    @property
    def quantity_available(self) -> int:
        return self.quantity - self.quantity_reserved

    def nav(self, price: Decimal) -> Decimal:
        money(price)
        return self.cash_total + self.quantity * price

    def validate(self) -> None:
        for value in (self.principal, self.cash_total, self.cash_reserved,
            self.cash_unsettled):
            money(value)
        if (not isinstance(self.position_cost, Decimal) or not self.position_cost.is_finite()
                or self.position_cost < 0):
            raise DomainError("Position cost must be a finite nonnegative Decimal")
        if self.cash_available < 0:
            raise DomainError("Reserved and unsettled cash exceed total cash")
        if (type(self.quantity) is not int or type(self.quantity_reserved) is not int
                or self.quantity_reserved < 0 or self.quantity_available < 0):
            raise DomainError("Invalid position reservation")


@dataclass
class Account:
    id: str
    unallocated_cash: Decimal
    currency: str = "KRW"
    execution_mode: str = "PAPER"
    gate_state: str = "CLOSED"
    revision: int = 0
    lease_token: str | None = None
    lease_until: datetime | None = None

    def validate(self) -> None:
        money(self.unallocated_cash)
        if self.currency != "KRW" or self.execution_mode not in {"PAPER", "BACKTEST"}:
            raise DomainError("Initial core supports only KRW PAPER/BACKTEST accounts")
        if self.gate_state not in {"OPEN", "CLOSED", "RECONCILIATION_REQUIRED"}:
            raise DomainError("Invalid execution gate")
        if self.lease_until is not None:
            aware(self.lease_until)


@dataclass(frozen=True)
class LedgerEntry:
    account_id: str
    allocation_id: str | None
    bucket: str
    signed_amount: Decimal
    currency: str = "KRW"


@dataclass(frozen=True)
class LedgerTransaction:
    id: str
    reason: str
    effective_at: datetime
    recorded_at: datetime
    actor: str
    correlation_id: str
    entries: tuple[LedgerEntry, ...]

    def __post_init__(self) -> None:
        aware(self.effective_at)
        aware(self.recorded_at)
        if not self.entries or len({e.account_id for e in self.entries}) != 1:
            raise DomainError("Ledger transaction must belong to one account")
        if any(e.currency != "KRW" or not isinstance(e.signed_amount, Decimal)
               or not e.signed_amount.is_finite()
               or e.signed_amount != e.signed_amount.to_integral_value() for e in self.entries):
            raise DomainError("Invalid ledger money")
        if sum((e.signed_amount for e in self.entries), Decimal(0)) != 0:
            raise DomainError("Ledger entries must balance")


@dataclass
class State:
    accounts: dict[str, Account] = field(default_factory=dict)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    allocations: dict[str, Allocation] = field(default_factory=dict)
    transactions: list[LedgerTransaction] = field(default_factory=list)
    schedules: dict = field(default_factory=dict)
    occurrences: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    assignments: dict = field(default_factory=dict)
    assignment_history: dict = field(default_factory=dict)
    evaluations: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)
    fills: dict = field(default_factory=dict)
    outbox: dict = field(default_factory=dict)
    risk_policies: dict = field(default_factory=dict)
    daily_usage: dict = field(default_factory=dict)
    simulation_runs: dict = field(default_factory=dict)
    marks: dict = field(default_factory=dict)

    def validate(self) -> None:
        for account in self.accounts.values():
            account.validate()
        scopes = set()
        for allocation in self.allocations.values():
            allocation.validate()
            if allocation.account_id not in self.accounts or allocation.instrument_id not in self.instruments:
                raise DomainError("Allocation references missing account/instrument")
            scope = (allocation.account_id, allocation.instrument_id)
            if scope in scopes:
                raise Conflict("One allocation per account and instrument")
            scopes.add(scope)
        from .trading import validate_trading_state
        validate_trading_state(self)
