import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D

from quantro.domain.models import DomainError, Instrument
from quantro.domain.schedule import Frequency
from quantro.domain.trading import MarketBar, RiskPolicy
from quantro.simulation.engine import BacktestEngine, DepositEvent, RunConfig


def time(day, hour=15):
    return datetime.fromisoformat(f"2026-01-{day:02d}T{hour:02d}:30:00+09:00")


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.config = RunConfig(time(5, 8), time(10, 8), D(1000000), D(100000),
            Instrument("etf", "TEST", "KRX"), "budget-buy", "1.0.0",
            (("ratio", "1"), ("repeat", False)), RiskPolicy("test", D(1000000), D(1000000), 60, D(0)),
            "fixture-v1", "fixture-code-v1", D(1), D(0), D(0))
        self.bars = tuple(MarketBar(f"bar:{day}", "etf", time(day, 9), time(day), time(day),
                                   D(100), D(110), D(90), D(100), 10000) for day in range(5, 10))

    def test_at14_deterministic_manifest_trades_and_checksum(self):
        first = BacktestEngine().run(self.config, self.bars)
        second = BacktestEngine().run(self.config, self.bars)
        self.assertEqual(first, second)
        self.assertEqual(first.metrics["fill_count"], 1)
        self.assertEqual(first.metrics["profit"], "0")
        self.assertIn("2026-01-06", first.trades_json)

    def test_at16_deposits_are_not_profit(self):
        config = replace(self.config, deposits=(DepositEvent(time(6, 8), D(300000)),))
        result = BacktestEngine().run(config, self.bars)
        self.assertEqual(result.metrics["net_contributions"], "300000")
        self.assertEqual(D(result.metrics["profit"]), 0)
        self.assertEqual(D(result.metrics["twr"]), 0)

    def test_internal_budgets_are_not_deposits(self):
        config = replace(self.config, initial_cash=D(100000), initial_budget=D(0),
                         schedule_amount=D(60000), schedule_frequency=Frequency.DAILY)
        result = BacktestEngine().run(config, self.bars)
        self.assertEqual(result.metrics["net_contributions"], "0")
        self.assertEqual(result.metrics["allocated_principal"], "60000")
        self.assertGreater(D(result.metrics["pending_budget"]), 0)

    def test_future_bars_are_not_used_for_evaluation(self):
        changed = self.bars[:-1] + (replace(self.bars[-1], close=D(110)),)
        first, second = [BacktestEngine().run(self.config, bars) for bars in (self.bars, changed)]
        self.assertEqual(first.valuations[:-1], second.valuations[:-1])
        self.assertNotEqual(first.run_id, second.run_id)

    def test_zero_volume_and_adverse_slippage_do_not_violate_limits(self):
        zero = tuple(replace(b, volume=0) for b in self.bars)
        self.assertEqual(BacktestEngine().run(self.config, zero).metrics["fill_count"], 0)
        config = replace(self.config, slippage_rate=D(".1"))
        self.assertEqual(BacktestEngine().run(config, self.bars).metrics["fill_count"], 0)

    def test_cancellation_at_event_boundary(self):
        result = BacktestEngine().run(self.config, self.bars, cancel_requested=lambda: True)
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.valuations, ())

    def test_duplicate_data_rejected(self):
        with self.assertRaises(DomainError):
            BacktestEngine().run(self.config, (self.bars[0], self.bars[0]))
