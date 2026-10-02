import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

from quantro.application.budget import BudgetService
from quantro.application.trading import TradingService
from quantro.domain.models import Conflict, DomainError, Instrument
from quantro.domain.trading import Fill, MarketBar, OrderState, Quote, RiskPolicy
from quantro.infrastructure.memory import MemoryStore
from quantro.ports.broker import AccountSnapshot, OrderSnapshot
from quantro.simulation.simulated_broker import SimulatedBroker
from quantro.strategies.examples import default_registry


class Clock:
    time = datetime.fromisoformat("2026-01-05T15:30:00+09:00")

    def now(self):
        return self.time


def bar(day, *, price=100, volume=10000):
    start = datetime.fromisoformat(f"2026-01-{day:02d}T09:00:00+09:00")
    end = start.replace(hour=15, minute=30)
    return MarketBar(f"bar:{day}", "etf", start, end, end, D(price), D(price + 10),
                     D(price - 10), D(price), volume)


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.store = self.make_store()
        self.clock = Clock()
        self.budget = BudgetService(self.store.unit_of_work, self.clock)
        self.registry = default_registry()
        self.trading = TradingService(self.store.unit_of_work, self.clock, self.registry)
        self.budget.create_account("account", D(1000000))
        self.budget.register_instrument(Instrument("etf", "TEST", "KRX"))
        self.allocation = self.budget.create_allocation("account", "etf", "Test")
        self.budget.add_budget(self.allocation.id, D(100000), currency="KRW", expected_revision=0, key="initial")
        self.assignment = self.trading.assign(self.allocation.id, "budget-buy", "1.0.0", {"ratio": "1", "repeat": True}, 0)
        self.policy = RiskPolicy("test-v1", D(1000000), D(1000000), 60, D(0))
        self.trading.configure_risk("account", self.policy)
        self.broker = SimulatedBroker(execution_mode="PAPER", participation=D(1), slippage_rate=D(0), sell_tax_rate=D(0))
        self.trading.start("account", self.broker, lease_token="worker")

    def make_store(self):
        return MemoryStore()

    def evaluate(self, day=5, **kwargs):
        event = bar(day)
        self.clock.time = event.available_at
        return self.trading.evaluate(self.allocation.id, event.id, (event,),
            kwargs.get("quote", Quote("etf", D(100), D(100), self.clock.now())),
            AccountSnapshot("account", D(1000000), self.clock.now()), lease_token=kwargs.get("token", "worker"))

    def order(self):
        evaluation = self.evaluate()
        self.trading.dispatch(evaluation.order_id, self.broker, lease_token="worker")
        return self.store.snapshot().orders[evaluation.order_id]

    def fill(self, order, quantity, execution="fill", **kwargs):
        return Fill("simulated", execution, order.id, quantity, D(kwargs.get("price", 100)),
                    D(kwargs.get("fee", 0)), D(0), self.clock.now(), kwargs.get("settlement", self.clock.now()))

    def test_reservation_does_not_create_position_and_evaluation_is_idempotent(self):
        evaluation = self.evaluate()
        self.assertEqual(self.evaluate(), evaluation)
        state = self.store.snapshot()
        self.assertEqual(len(state.orders), 1)
        self.assertEqual(state.allocations[self.allocation.id].quantity, 0)
        self.assertEqual(state.allocations[self.allocation.id].cash_reserved, D(100000))

    def test_at08_fill_duplicate_and_conflicting_payload(self):
        order = self.order()
        fill = self.fill(order, 1000)
        self.assertEqual(self.trading.record_fill(fill), self.trading.record_fill(fill))
        state = self.store.snapshot()
        allocation = state.allocations[self.allocation.id]
        self.assertEqual((allocation.quantity, allocation.cash_total, allocation.cash_reserved), (1000, D(0), D(0)))
        self.assertEqual(state.orders[order.id].state, OrderState.FILLED)
        with self.assertRaises(Conflict):
            self.trading.record_fill(replace(fill, price=D(99)))

    def test_at10_timeout_retains_reservation_and_does_not_resubmit(self):
        class TimeoutBroker(SimulatedBroker):
            calls = 0
            def submit(self, request):
                self.calls += 1
                super().submit(request)
                raise TimeoutError()
        broker = TimeoutBroker(execution_mode="PAPER", participation=D(1), slippage_rate=D(0), sell_tax_rate=D(0))
        evaluation = self.evaluate()
        self.trading.dispatch(evaluation.order_id, broker, lease_token="worker")
        state = self.store.snapshot()
        self.assertEqual(state.orders[evaluation.order_id].state, OrderState.UNKNOWN)
        self.assertEqual(state.allocations[self.allocation.id].cash_reserved, D(100000))
        self.assertFalse(self.trading.dispatch(evaluation.order_id, broker, lease_token="worker"))
        self.assertEqual(broker.calls, 1)
        self.assertTrue(self.trading.recover(evaluation.order_id, broker))
        self.assertEqual(self.store.snapshot().orders[evaluation.order_id].state, OrderState.ACKNOWLEDGED)

    def test_at11_partial_fill_cancel_requires_confirmed_snapshot(self):
        order = self.order()
        fill = self.fill(order, 100)
        self.trading.record_fill(fill)
        # Mirror the external broker's execution history before cancel confirmation.
        self.broker.orders[order.id].fills.append(fill)
        self.broker.orders[order.id].state = OrderState.PARTIALLY_FILLED
        self.trading.cancel(order.id, self.broker)
        self.assertEqual(self.store.snapshot().allocations[self.allocation.id].cash_reserved, D(90000))
        self.trading.recover(order.id, self.broker)
        allocation = self.store.snapshot().allocations[self.allocation.id]
        self.assertEqual((allocation.quantity, allocation.cash_reserved, allocation.cash_total), (100, D(0), D(90000)))

    def test_at12_stop_blocks_undispatched_outbox(self):
        evaluation = self.evaluate()
        self.trading.stop("account")
        self.assertFalse(self.trading.dispatch(evaluation.order_id, self.broker, lease_token="worker"))
        self.assertEqual(self.broker.orders, {})
        self.assertEqual(self.store.snapshot().orders[evaluation.order_id].state, OrderState.RESERVED)

    def test_at13_next_bar_fill_and_partial_day_expiration(self):
        order = self.order()
        self.assertEqual(self.broker.on_bar(bar(5)), ())
        snapshot = self.broker.on_bar(bar(6, volume=50))[0]
        self.assertEqual(snapshot.filled_quantity, 50)
        self.assertEqual(snapshot.state, OrderState.EXPIRED)
        self.clock.time = bar(6).available_at
        self.trading.recover(order.id, self.broker)
        self.assertEqual(self.store.snapshot().allocations[self.allocation.id].cash_reserved, D(0))

    def test_at18_assignment_history_preserved(self):
        old = self.assignment
        new = self.trading.update_assignment(old.id, config={"ratio": "0.5", "repeat": False},
                                             priority=1, enabled=True, expected_revision=0)
        self.assertEqual(new.revision, 1)
        self.assertEqual(self.store.snapshot().assignment_history[(old.id, 0)], old)
        with self.assertRaises(Conflict):
            self.trading.update_assignment(old.id, config=dict(old.config), priority=0, enabled=True, expected_revision=0)

    def test_priority_hold_does_not_block_lower_strategy(self):
        self.trading.update_assignment(self.assignment.id, config=dict(self.assignment.config),
                                      priority=1, enabled=True, expected_revision=0)
        self.trading.assign(self.allocation.id, "moving-average", "1.0.0",
                            {"period": 2, "buy_ratio": "1", "sell_ratio": "1", "repeat": False}, 0)
        result = self.evaluate()
        self.assertEqual(result.selected.strategy_assignment_id, self.assignment.id)

    def test_suppression_and_distinct_priorities(self):
        low = self.trading.assign(self.allocation.id, "budget-buy", "1.0.0", {"ratio": "0.5", "repeat": True}, 1)
        result = self.evaluate()
        self.assertEqual(result.selected.strategy_assignment_id, self.assignment.id)
        self.assertEqual(result.suppressed[0].strategy_assignment_id, low.id)
        with self.assertRaises(DomainError):
            self.trading.assign(self.allocation.id, "budget-buy", "1.0.0", {"ratio": "1", "repeat": True}, 1)

    def test_limits_staleness_and_lease_fencing(self):
        self.trading.configure_risk("account", replace(self.policy, max_order_amount=D(100)))
        self.assertEqual(self.evaluate().reason, "ORDER_LIMIT")
        self.trading.configure_risk("account", self.policy)
        self.assertEqual(self.evaluate(6).reason, "LEASE_EXPIRED")
        self.trading.start("account", self.broker, lease_token="worker")
        self.assertEqual(self.evaluate(6, token="other").reason, "LEASE_EXPIRED")  # existing evaluation stays immutable
        self.clock.time = bar(7).available_at
        self.trading.start("account", self.broker, lease_token="worker")
        stale = Quote("etf", D(100), D(100), self.clock.now() - timedelta(seconds=61))
        self.assertEqual(self.evaluate(7, quote=stale).reason, "STALE_DATA")

    def test_sell_settlement_and_cash_cannot_use_unsettled_proceeds(self):
        buy = self.order()
        self.trading.record_fill(self.fill(buy, 1000))
        self.trading.update_assignment(self.assignment.id,
            config={"ratio": "1", "repeat": False}, priority=0, enabled=False, expected_revision=0)
        self.trading.assign(self.allocation.id, "moving-average", "1.0.0",
            {"period": 2, "buy_ratio": "1", "sell_ratio": "1", "repeat": False}, 1)
        self.clock.time = bar(6).available_at
        self.trading.start("account", self.broker, lease_token="worker")
        event = bar(6, price=90)
        result = self.trading.evaluate(self.allocation.id, event.id, (bar(5), event),
            Quote("etf", D(90), D(90), self.clock.now()), AccountSnapshot("account", D(900000), self.clock.now()), lease_token="worker")
        self.trading.dispatch(result.order_id, self.broker, lease_token="worker")
        sell = self.store.snapshot().orders[result.order_id]
        settlement = self.clock.now() + timedelta(days=2)
        self.trading.record_fill(self.fill(sell, 1000, "sell", price=90, settlement=settlement))
        allocation = self.store.snapshot().allocations[self.allocation.id]
        self.assertEqual((allocation.cash_total, allocation.cash_available, allocation.position_cost), (D(90000), D(0), D(0)))
        self.trading.settle(settlement)
        self.trading.settle(settlement)
        self.assertEqual(self.store.snapshot().allocations[self.allocation.id].cash_available, D(90000))

    def test_oversized_fill_rolls_back(self):
        order = self.order()
        before = self.store.snapshot()
        with self.assertRaises(Conflict):
            self.trading.record_fill(self.fill(order, 1001))
        self.assertEqual(self.store.snapshot(), before)

    def test_invalid_strategy_config_rejected(self):
        for config in ({"ratio": "0", "repeat": True}, {"ratio": 0.1, "repeat": True}, {"ratio": "1"}):
            with self.assertRaises(DomainError):
                self.trading.assign(self.allocation.id, "budget-buy", "1.0.0", config, 1)
