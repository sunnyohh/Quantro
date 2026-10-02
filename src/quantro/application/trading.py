from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from quantro.domain.models import Conflict, DomainError, LedgerEntry, LedgerTransaction, aware, money
from quantro.domain.sizing import size_buy, size_sell
from quantro.domain.strategy import StrategyAssignment, StrategyContext
from quantro.domain.trading import (Evaluation, Fill, FillRecord, MarketBar, Order,
    OrderState, OutboxEvent, Quote, RiskPolicy, Side, TERMINAL)
from quantro.ports.broker import AccountSnapshot, BrokerPort, OrderRequest
from quantro.ports.clock import Clock
from quantro.ports.unit_of_work import UnitOfWork
from quantro.strategies.registry import StrategyRegistry


class TradingService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock,
                 registry: StrategyRegistry, id_factory: Callable[[], str] = lambda: str(uuid4())):
        self.uow_factory, self.clock, self.registry, self.id_factory = uow_factory, clock, registry, id_factory

    def assign(self, allocation_id: str, plugin_id: str, version: str, config: dict,
               priority: int) -> StrategyAssignment:
        self.registry.validate_config(plugin_id, version, config)
        if type(priority) is not int:
            raise DomainError("Priority must be an integer; smaller numbers run first")
        with self.uow_factory() as uow:
            if allocation_id not in uow.state.allocations:
                raise DomainError("Unknown allocation")
            assignment = StrategyAssignment(self.id_factory(), allocation_id, plugin_id, version,
                                            tuple(sorted(config.items())), priority)
            uow.state.assignments[assignment.id] = assignment
            uow.state.assignment_history[(assignment.id, assignment.revision)] = assignment
            uow.commit()
            return assignment

    def update_assignment(self, assignment_id: str, *, config: dict, priority: int,
                          enabled: bool, expected_revision: int, archived: bool = False) -> StrategyAssignment:
        with self.uow_factory() as uow:
            old = uow.state.assignments[assignment_id]
            if old.revision != expected_revision:
                raise Conflict("Stale strategy assignment revision")
            self.registry.validate_config(old.plugin_id, old.plugin_version, config)
            if type(priority) is not int or type(enabled) is not bool or type(archived) is not bool:
                raise DomainError("Invalid assignment update")
            if old.archived:
                raise Conflict("Archived strategy assignment cannot be edited")
            new = replace(old, config=tuple(sorted(config.items())), priority=priority,
                          enabled=enabled and not archived, archived=archived, revision=old.revision + 1)
            uow.state.assignments[assignment_id] = new
            uow.state.assignment_history[(new.id, new.revision)] = new
            uow.commit()
            return new

    def configure_risk(self, account_id: str, policy: RiskPolicy) -> None:
        with self.uow_factory() as uow:
            if account_id not in uow.state.accounts:
                raise DomainError("Unknown account")
            uow.state.risk_policies[account_id] = policy
            uow.commit()

    def start(self, account_id: str, broker: BrokerPort, *, lease_token: str,
              lease_seconds: int = 60) -> None:
        if not lease_token or type(lease_seconds) is not int or lease_seconds <= 0:
            raise DomainError("Explicit worker lease is required")
        if not broker.ready():
            raise Conflict("Broker is not ready")
        now = aware(self.clock.now())
        with self.uow_factory() as uow:
            account = uow.state.accounts[account_id]
            if account.execution_mode != broker.execution_mode:
                raise Conflict("Broker and account execution modes differ")
            if account.gate_state == "RECONCILIATION_REQUIRED":
                raise Conflict("Account reconciliation is required")
            if account_id not in uow.state.risk_policies:
                raise Conflict("Explicit risk limits are required")
            if account.lease_until is not None and account.lease_until > now and account.lease_token != lease_token:
                raise Conflict("Account already has an active writer lease")
            if any(o.account_id == account_id and o.state in {OrderState.SUBMITTING, OrderState.UNKNOWN}
                   for o in uow.state.orders.values()):
                raise Conflict("Recover uncertain orders before starting")
            account.lease_token, account.lease_until = lease_token, now + timedelta(seconds=lease_seconds)
            account.gate_state = "OPEN"
            account.revision += 1
            uow.commit()

    def stop(self, account_id: str) -> None:
        with self.uow_factory() as uow:
            account = uow.state.accounts[account_id]
            if account.gate_state != "RECONCILIATION_REQUIRED":
                account.gate_state = "CLOSED"
            account.revision += 1
            uow.commit()

    def evaluate(self, allocation_id: str, market_event_id: str, bars: tuple[MarketBar, ...],
                 quote: Quote, snapshot: AccountSnapshot, *, lease_token: str) -> Evaluation:
        now = aware(self.clock.now())
        with self.uow_factory() as uow:
            allocation = uow.state.allocations[allocation_id]
            assignments = sorted((a for a in uow.state.assignments.values()
                if a.allocation_id == allocation_id and a.enabled and not a.archived), key=lambda a: a.priority)
            revisions = tuple((a.id, a.revision) for a in assignments)
            # A confirmed allocation/event is consumed once, including across assignment edits.
            key = (allocation_id, market_event_id)
            if key in uow.state.evaluations:
                return deepcopy(uow.state.evaluations[key])
            evaluation_id = self.id_factory()
            window = tuple(b for b in bars if b.instrument_id == allocation.instrument_id and b.available_at <= now)
            if any(window[i].available_at >= window[i + 1].available_at for i in range(len(window) - 1)):
                raise DomainError("Market window must be strictly ordered")
            if not window or window[-1].id != market_event_id:
                raise DomainError("Evaluation must reference the latest available confirmed bar")
            uow.state.marks[allocation.instrument_id] = (window[-1].close, window[-1].available_at)
            digest = sha256(repr((window, allocation, tuple(assignments))).encode()).hexdigest()
            intents = []
            for assignment in assignments:
                plugin = self.registry.get(assignment.plugin_id, assignment.plugin_version)
                context = StrategyContext(evaluation_id, assignment.id, allocation_id, now, window,
                    allocation.principal, allocation.cash_available, allocation.quantity_available, assignment.config)
                try:
                    result = tuple(plugin.evaluate(context))
                    if len(result) > 1 or any(i.allocation_id != allocation_id or i.strategy_assignment_id != assignment.id
                        or i.evaluation_id != evaluation_id for i in result):
                        raise DomainError("Strategy returned conflicting or incorrectly scoped intents")
                    intents.extend(result)
                except Exception as exc:
                    evaluation = Evaluation(evaluation_id, allocation_id, market_event_id, now, revisions,
                                             digest, "ERROR", type(exc).__name__)
                    uow.state.evaluations[key] = evaluation
                    uow.commit()
                    return evaluation
            selected = intents[0] if intents else None
            suppressed = tuple(intents[1:])
            policy = uow.state.risk_policies.get(allocation.account_id)
            reason = "HOLD" if selected is None else self._risk_reason(uow, allocation, quote, snapshot, lease_token, now)
            order = None
            if selected is not None and not reason:
                instrument = uow.state.instruments[allocation.instrument_id]
                price = quote.ask if selected.side == Side.BUY else quote.bid
                if selected.side == Side.BUY:
                    power = snapshot.buying_power
                    if not snapshot.includes_local_reservations:
                        power -= sum((a.cash_reserved for a in uow.state.allocations.values()
                                      if a.account_id == allocation.account_id), Decimal(0))
                    sized = size_buy(allocation, instrument, selected.ratio, price, max(Decimal(0), power),
                                     lambda q: policy.fee_upper_bound(q, price))
                    quantity, target, cash, qty_reserved = sized.quantity, sized.target_amount, sized.reserved_amount, 0
                    sizing_reason = sized.reason
                else:
                    quantity = size_sell(allocation, instrument, selected.ratio)
                    target, cash, qty_reserved = quantity * price, Decimal(0), quantity
                    sizing_reason = "SIZED" if quantity else "SKIPPED"
                usage_key = (allocation.account_id, now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat())
                amount = cash if selected.side == Side.BUY else quantity * price
                if not quantity:
                    reason = "ZERO_QUANTITY"
                elif amount > policy.max_order_amount:
                    reason = "ORDER_LIMIT"
                elif uow.state.daily_usage.get(usage_key, Decimal(0)) + amount > policy.max_daily_amount:
                    reason = "DAILY_LIMIT"
                else:
                    order_id = self.id_factory()
                    order = Order(order_id, order_id, allocation_id, allocation.account_id,
                        allocation.instrument_id, evaluation_id, selected.side, quantity, price, target,
                        cash, qty_reserved, policy.fee_rate, now)
                    allocation.cash_reserved += cash
                    allocation.quantity_reserved += qty_reserved
                    allocation.revision += 1
                    uow.state.orders[order.id] = order
                    event = OutboxEvent(self.id_factory(), order.id, now)
                    uow.state.outbox[event.id] = event
                    uow.state.daily_usage[usage_key] = uow.state.daily_usage.get(usage_key, Decimal(0)) + amount
                    reason = sizing_reason
            status = "ORDERED" if order else ("HOLD" if selected is None else "SKIPPED")
            evaluation = Evaluation(evaluation_id, allocation_id, market_event_id, now, revisions, digest,
                status, reason, selected, suppressed, order.id if order else None, policy.version if policy else None)
            uow.state.evaluations[key] = evaluation
            uow.commit()
            return evaluation

    def _risk_reason(self, uow, allocation, quote, snapshot, token, now):
        account = uow.state.accounts[allocation.account_id]
        policy = uow.state.risk_policies.get(account.id)
        if account.gate_state != "OPEN":
            return "GATE_CLOSED"
        if account.lease_token != token or account.lease_until is None or account.lease_until <= now:
            return "LEASE_EXPIRED"
        if policy is None:
            return "RISK_NOT_CONFIGURED"
        if any(o.allocation_id == allocation.id and o.state not in TERMINAL for o in uow.state.orders.values()):
            return "ORDER_IN_PROGRESS"
        if quote.instrument_id != allocation.instrument_id or snapshot.account_id != account.id:
            return "SCOPE_MISMATCH"
        money(snapshot.buying_power)
        aware(snapshot.as_of)
        for time in (quote.as_of, snapshot.as_of):
            age = (now - time).total_seconds()
            if age < 0 or age > policy.max_quote_age_seconds:
                return "STALE_DATA"
        if not quote.trading or not quote.session_open:
            return "SESSION_CLOSED"
        return ""

    def dispatch(self, order_id: str, broker: BrokerPort, *, lease_token: str) -> bool:
        if not broker.ready():
            return False
        now = aware(self.clock.now())
        with self.uow_factory() as uow:
            order = uow.state.orders[order_id]
            account = uow.state.accounts[order.account_id]
            if (order.state != OrderState.RESERVED or account.gate_state != "OPEN"
                or account.execution_mode != broker.execution_mode or account.lease_token != lease_token
                or account.lease_until is None or account.lease_until <= now):
                return False
            order.transition(OrderState.SUBMITTING)
            for event in uow.state.outbox.values():
                if event.order_id == order_id:
                    event.dispatched = True
            request = OrderRequest(order.id, order.client_order_id, order.account_id, order.instrument_id,
                                  order.side, order.quantity, order.limit_price, order.fee_rate, now)
            uow.commit()
        # Never hold a database transaction open across an external SDK call.
        try:
            result = broker.submit(request)
        except Exception:
            result = None
        with self.uow_factory() as uow:
            order = uow.state.orders[order_id]
            # A fill may have beaten the acknowledgement.
            if order.state != OrderState.SUBMITTING:
                return True
            if result is not None and result.status == "ACCEPTED":
                order.broker_order_id = result.broker_order_id
                order.transition(OrderState.ACKNOWLEDGED)
            elif result is not None and result.status == "REJECTED":
                order.transition(OrderState.REJECTED)
                self._release(uow, order)
            else:
                order.transition(OrderState.UNKNOWN)
            uow.commit()
        return True

    def record_fill(self, fill: Fill) -> FillRecord:
        with self.uow_factory() as uow:
            key = (fill.broker_id, fill.execution_id)
            if key in uow.state.fills:
                record = uow.state.fills[key]
                if record.fill != fill:
                    raise Conflict("Execution id reused with a different fill")
                return deepcopy(record)
            order = uow.state.orders[fill.order_id]
            allocation = uow.state.allocations[order.allocation_id]
            if fill.trade_at < order.created_at or fill.quantity > order.remaining:
                raise Conflict("Fill timestamp or quantity conflicts with order")
            if order.state in {OrderState.RESERVED, OrderState.REJECTED, OrderState.FILLED}:
                raise Conflict("Fill cannot be applied to this order state")
            if (order.side == Side.BUY and fill.price > order.limit_price
                    or order.side == Side.SELL and fill.price < order.limit_price):
                raise Conflict("Fill violates limit price")
            late = order.state in {OrderState.CANCELED, OrderState.EXPIRED}
            gross = fill.quantity * fill.price
            cost = fill.fee + fill.tax
            entries = []
            def entry(bucket, amount, scope=None):
                entries.append(LedgerEntry(order.account_id, scope, bucket, amount))
            if order.side == Side.BUY:
                unit_cap = order.limit_price + (order.limit_price * order.fee_rate).to_integral_value(rounding=ROUND_CEILING)
                released = min(order.reserved_cash, fill.quantity * unit_cap)
                if gross + cost > (allocation.cash_available if late else released):
                    raise Conflict("Fill exceeds funded reservation; reconciliation required")
                allocation.cash_total -= gross + cost
                allocation.cash_reserved -= released
                order.reserved_cash -= released
                allocation.quantity += fill.quantity
                allocation.position_cost += gross + cost
                entry("allocation_cash", -gross - cost, allocation.id)
                entry("trade_clearing", gross)
            else:
                if fill.quantity > (allocation.quantity_available if late else order.reserved_quantity):
                    raise Conflict("Fill exceeds held quantity")
                if cost > gross:
                    raise Conflict("Sell costs exceed proceeds")
                original_quantity = allocation.quantity
                allocation.position_cost = (Decimal(0) if fill.quantity == original_quantity else
                    allocation.position_cost * (original_quantity - fill.quantity) / original_quantity)
                allocation.quantity -= fill.quantity
                released = min(order.reserved_quantity, fill.quantity)
                allocation.quantity_reserved -= released
                order.reserved_quantity -= released
                allocation.cash_total += gross - cost
                if fill.settlement_at > self.clock.now():
                    allocation.cash_unsettled += gross - cost
                entry("allocation_cash", gross - cost, allocation.id)
                entry("trade_clearing", -gross)
            entry("fees", fill.fee)
            entry("taxes", fill.tax)
            transaction = LedgerTransaction(self.id_factory(), "FILL", fill.trade_at, self.clock.now(),
                                            "broker", fill.execution_id, tuple(entries))
            uow.state.transactions.append(transaction)
            order.filled_quantity += fill.quantity
            if late:
                # Preserve the broker terminal status for a partially filled canceled order.
                if not order.remaining:
                    order.state = OrderState.FILLED
                order.revision += 1
                uow.state.accounts[order.account_id].gate_state = "RECONCILIATION_REQUIRED"
            else:
                state = OrderState.FILLED if not order.remaining else OrderState.PARTIALLY_FILLED
                if order.state == OrderState.CANCEL_PENDING and order.remaining:
                    order.revision += 1  # Retain pending cancel while fills continue to arrive.
                else:
                    order.transition(state)
            if not order.remaining:
                self._release(uow, order)
            allocation.revision += 1
            record = FillRecord(fill, transaction.id,
                                order.side == Side.BUY or fill.settlement_at <= self.clock.now())
            uow.state.fills[key] = record
            uow.commit()
            return deepcopy(record)

    def settle(self, through: datetime) -> None:
        aware(through)
        with self.uow_factory() as uow:
            for record in uow.state.fills.values():
                if not record.settled and record.fill.settlement_at <= through:
                    fill = record.fill
                    allocation = uow.state.allocations[uow.state.orders[fill.order_id].allocation_id]
                    allocation.cash_unsettled -= fill.quantity * fill.price - fill.fee - fill.tax
                    allocation.revision += 1
                    record.settled = True
            uow.commit()

    def cancel(self, order_id: str, broker: BrokerPort) -> None:
        local = False
        with self.uow_factory() as uow:
            order = uow.state.orders[order_id]
            if order.state in TERMINAL or order.state == OrderState.CANCEL_PENDING:
                return
            if order.state == OrderState.RESERVED:
                order.transition(OrderState.CANCELED)
                self._release(uow, order)
                local = True
            elif order.state in {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}:
                order.transition(OrderState.CANCEL_PENDING)
            else:
                raise Conflict("Query uncertain order before canceling")
            uow.commit()
        if not local:
            broker.cancel(order_id)
            # A cancel response alone does not release reservations.

    def recover(self, order_id: str, broker: BrokerPort) -> bool:
        snapshot = broker.query_order(order_id)
        if snapshot.order_id != order_id:
            raise Conflict("Broker returned a different order")
        for fill in snapshot.fills:
            if fill.order_id != order_id or fill.broker_id != broker.broker_id:
                raise Conflict("Broker returned incorrectly scoped fill")
            self.record_fill(fill)
        with self.uow_factory() as uow:
            order = uow.state.orders[order_id]
            if order.filled_quantity != snapshot.filled_quantity:
                account = uow.state.accounts[order.account_id]
                account.gate_state = "RECONCILIATION_REQUIRED"
                uow.commit()
                return False
            if snapshot.state == OrderState.FILLED and order.remaining:
                uow.state.accounts[order.account_id].gate_state = "RECONCILIATION_REQUIRED"
                uow.commit()
                return False
            if snapshot.state == OrderState.UNKNOWN:
                if order.state == OrderState.SUBMITTING:
                    order.transition(OrderState.UNKNOWN)
                uow.commit()
                return False
            order.broker_order_id = snapshot.broker_order_id
            if snapshot.state != order.state:
                order.transition(snapshot.state)
            if order.state in TERMINAL:
                self._release(uow, order)
            uow.commit()
            return True

    def _release(self, uow, order):
        allocation = uow.state.allocations[order.allocation_id]
        allocation.cash_reserved -= order.reserved_cash
        allocation.quantity_reserved -= order.reserved_quantity
        order.reserved_cash, order.reserved_quantity = Decimal(0), 0
        allocation.revision += 1
