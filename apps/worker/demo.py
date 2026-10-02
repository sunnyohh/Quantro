"""Run with an installed package: python apps/worker/demo.py."""
from datetime import datetime
from decimal import Decimal

from quantro.application.budget import BudgetService
from quantro.domain.models import Instrument
from quantro.domain.schedule import Frequency
from quantro.infrastructure.memory import MemoryStore


class DemoClock:
    time = datetime.fromisoformat("2026-01-01T09:00:00+09:00")

    def now(self):
        return self.time


def main():
    store, clock = MemoryStore(), DemoClock()
    service = BudgetService(store.unit_of_work, clock)
    service.create_account("demo", Decimal(10000000))
    service.register_instrument(Instrument("example-etf", "EXAMPLE", "KRX"))
    allocation = service.create_allocation("demo", "example-etf", "Example ETF")
    service.set_schedule(allocation.id, Decimal(300000), Frequency.WEEKLY, expected_revision=0)
    clock.time = datetime.fromisoformat("2026-01-22T09:00:00+09:00")
    service.process_due(clock.now())
    state = store.snapshot()
    print(f"Allocated principal: {state.allocations[allocation.id].principal:,} KRW")
    print(f"Unallocated cash: {state.accounts['demo'].unallocated_cash:,} KRW")


if __name__ == "__main__":
    main()
