import argparse
import os
import time

from quantro.application.budget import BudgetService
from quantro.application.simulation import SimulationService
from quantro.infrastructure.clock import SystemClock
from quantro.infrastructure.database.store import DatabaseStore


def main():
    parser = argparse.ArgumentParser(description="Quantro budget and isolated simulation worker")
    parser.add_argument("--once", action="store_true", help="Process budget catch-up and one queued simulation")
    args = parser.parse_args()
    store = DatabaseStore(os.environ["QUANTRO_DATABASE_URL"])
    if store.engine.dialect.name == "sqlite":
        store.initialize_local()
    clock = SystemClock()
    budget = BudgetService(store.unit_of_work, clock)
    simulation = SimulationService(store.unit_of_work, clock)
    try:
        while True:
            budget.process_due(clock.now())
            simulation.run_next()
            if args.once:
                break
            time.sleep(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
