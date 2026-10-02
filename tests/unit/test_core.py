import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal as D

from quantro.application.budget import BudgetService
from quantro.domain.models import Conflict, DomainError, Instrument, LedgerEntry, LedgerTransaction
from quantro.domain.schedule import Frequency
from quantro.domain.sizing import size_buy, size_sell
from quantro.infrastructure.memory import MemoryStore


class FixedClock:
    def __init__(self, time="2026-01-01T09:00:00+09:00"):
        self.time = datetime.fromisoformat(time)

    def now(self):
        return self.time


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.clock = FixedClock()
        self.service = BudgetService(self.store.unit_of_work, self.clock)
        self.service.create_account("account", D(10000000))
        self.instrument = Instrument("etf", "TEST", "KRX")
        self.service.register_instrument(self.instrument)
        self.allocation = self.service.create_allocation("account", "etf", "ETF")

    def current(self):
        return self.store.snapshot().allocations[self.allocation.id]

    def add(self, amount, key="add"):
        return self.service.add_budget(self.allocation.id, D(amount), currency="KRW",
            expected_revision=self.current().revision, key=key)

    def schedule(self, amount, frequency):
        return self.service.set_schedule(self.allocation.id, D(amount), frequency,
                                         expected_revision=self.current().revision)

    def advance(self, time):
        self.clock.time = datetime.fromisoformat(time)
        self.service.process_due(self.clock.now())

    def test_at01_to_at03_schedule_change_and_addition(self):
        self.schedule(300000, Frequency.WEEKLY)
        self.advance("2026-01-22T09:00:00+09:00")
        self.assertEqual(self.current().principal, D(900000))
        self.schedule(1000000, Frequency.MONTHLY)
        self.assertEqual(self.current().principal, D(900000))
        self.advance("2026-02-22T09:00:00+09:00")
        self.assertEqual(self.current().principal, D(1900000))
        self.advance("2026-03-22T09:00:00+09:00")
        self.assertEqual(self.current().principal, D(2900000))
        self.add(100000)
        self.assertEqual(self.current().principal, D(3000000))
        self.assertEqual(self.store.snapshot().accounts["account"].unallocated_cash, D(7000000))

    def test_at04_separate_allocation_budgets(self):
        self.add(1000000)
        self.service.register_instrument(Instrument("other", "OTHER", "KRX"))
        other = self.service.create_allocation("account", "other", "Other")
        self.service.add_budget(other.id, D(5000000), currency="KRW", expected_revision=0, key="other")
        state = self.store.snapshot()
        self.assertEqual(state.accounts["account"].unallocated_cash, D(4000000))
        self.assertEqual([a.principal / D(10000000) for a in state.allocations.values()], [D(".1"), D(".5")])
        self.assertEqual(self.current().cash_available, D(1000000))

    def test_at05_market_valuation_does_not_change_cash_or_budget(self):
        self.add(3000000)
        allocation = self.current()
        # A completed-fill snapshot; execution is implemented in the next stage.
        allocation.cash_total = D(2000000)
        allocation.quantity = 100
        allocation.position_cost = D(1000000)
        self.assertEqual(allocation.nav(D(11200)), D(3120000))
        self.assertEqual(allocation.cash_available, D(2000000))
        self.assertEqual(allocation.principal, D(3000000))

    def test_at06_buy_sizing(self):
        self.add(3000000)
        result = size_buy(self.current(), self.instrument, D(".1"), D(15300), D(3000000), lambda q: D(0))
        self.assertEqual((result.quantity, result.reserved_amount), (19, D(290700)))

    def test_at07_insufficient_schedule_is_deferred_then_retried(self):
        self.schedule(11000000, Frequency.WEEKLY)
        self.advance("2026-01-15T09:00:00+09:00")
        state = self.store.snapshot()
        self.assertEqual(self.current().principal, D(0))
        self.assertEqual(len(state.occurrences), 2)
        self.assertTrue(all(o.status == "PENDING_FUNDS" for o in state.occurrences.values()))
        self.service.deposit("account", D(1000000), key="deposit")
        self.service.process_due(self.clock.now())
        occurrences = sorted(self.store.snapshot().occurrences.values(), key=lambda o: o.due_at)
        self.assertEqual([o.status for o in occurrences], ["APPLIED", "PENDING_FUNDS"])
        self.assertEqual(self.current().principal, D(11000000))

    def test_at08_duplicate_command_and_occurrence(self):
        args = dict(currency="KRW", expected_revision=0, key="same")
        first = self.service.add_budget(self.allocation.id, D(100000), **args)
        second = self.service.add_budget(self.allocation.id, D(100000), **args)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.add_budget(self.allocation.id, D(200000), **args)
        self.schedule(300000, Frequency.WEEKLY)
        self.advance("2026-01-08T09:00:00+09:00")
        before = self.store.snapshot()
        self.service.process_due(self.clock.now())
        self.assertEqual(self.store.snapshot(), before)

    def test_at15_month_end_preserves_anchor_day(self):
        self.clock.time = datetime.fromisoformat("2026-01-31T09:00:00+09:00")
        schedule = self.schedule(100000, Frequency.MONTHLY)
        self.assertEqual(schedule.due_at(1), datetime(2026, 2, 28, tzinfo=timezone.utc))
        self.assertEqual(schedule.due_at(2), datetime(2026, 3, 31, tzinfo=timezone.utc))
        self.advance("2026-03-31T09:00:00+09:00")
        self.assertEqual(self.current().principal, D(200000))

    def test_occurrence_at_change_time_is_processed_first(self):
        self.schedule(300000, Frequency.WEEKLY)
        self.clock.time = datetime.fromisoformat("2026-01-08T09:00:00+09:00")
        self.schedule(1000000, Frequency.MONTHLY)
        self.assertEqual(self.current().principal, D(300000))
        self.advance("2026-02-08T09:00:00+09:00")
        self.assertEqual(self.current().principal, D(1300000))

    def test_concurrent_additions_cannot_overdraw(self):
        def attempt(index):
            try:
                self.service.add_budget(self.allocation.id, D(6000000), currency="KRW",
                    expected_revision=0, key=str(index))
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, range(2))), 1)
        self.assertEqual(self.current().principal, D(6000000))

    def test_rollback_and_invalid_inputs(self):
        before = self.store.snapshot()
        with self.assertRaises(Conflict):
            self.add(11000000)
        self.assertEqual(self.store.snapshot(), before)
        with self.assertRaises(Conflict):
            self.service.create_allocation("account", "etf", "duplicate")
        self.assertEqual(self.store.snapshot(), before)
        for amount in (1.0, D("NaN"), D("Infinity"), D(".5"), D(-1)):
            with self.assertRaises(DomainError):
                self.service.deposit("account", amount, key="invalid")

    def test_lot_fees_cash_and_broker_caps(self):
        self.add(3000000)
        instrument = Instrument("lots", "LOTS", "KRX", lot_size=10)
        result = size_buy(self.current(), instrument, D(1), D(100), D(3050), lambda q: D(q * 2))
        self.assertEqual(result.quantity, 20)
        self.assertEqual(result.reserved_amount, D(2040))
        self.assertEqual(result.reason, "CLIP_TO_AVAILABLE")
        allocation = self.current()
        allocation.quantity, allocation.quantity_reserved = 99, 19
        self.assertEqual(size_sell(allocation, instrument, D(".5")), 40)

    def test_ledger_balanced_and_defensive_snapshots(self):
        self.add(100000)
        for transaction in self.store.snapshot().transactions:
            self.assertEqual(sum(e.signed_amount for e in transaction.entries), 0)
        snapshot = self.store.snapshot()
        snapshot.accounts["account"].unallocated_cash = D(0)
        self.assertEqual(self.store.snapshot().accounts["account"].unallocated_cash, D(9900000))
        with self.assertRaises(DomainError):
            LedgerTransaction("bad", "TEST", self.clock.now(), self.clock.now(), "test", "test",
                              (LedgerEntry("account", None, "cash", D(1)),))


if __name__ == "__main__":
    unittest.main()
