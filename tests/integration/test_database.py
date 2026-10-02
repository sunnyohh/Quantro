import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal as D

from quantro.application.budget import BudgetService
from quantro.domain.models import Conflict, Instrument
from quantro.domain.schedule import Frequency
from quantro.infrastructure.database.store import DatabaseStore


class Clock:
    time = datetime.fromisoformat("2026-01-01T09:00:00+09:00")
    def now(self):
        return self.time


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.temp.name) / "quantro.db").as_posix()
        self.store = DatabaseStore(self.url)
        self.store.initialize_local()
        self.clock = Clock()
        self.service = BudgetService(self.store.unit_of_work, self.clock)
        self.service.create_account("account", D(1000000))
        self.service.register_instrument(Instrument("etf", "TEST", "KRX"))
        self.allocation = self.service.create_allocation("account", "etf", "Test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_restart_recovers_occurrences_and_command_results(self):
        self.service.add_budget(self.allocation.id, D(100000), currency="KRW", expected_revision=0, key="initial")
        self.service.set_schedule(self.allocation.id, D(300000), Frequency.WEEKLY, expected_revision=1)
        self.clock.time = datetime.fromisoformat("2026-01-15T09:00:00+09:00")
        self.service.process_due(self.clock.now())
        before = self.store.snapshot()
        self.store.close()
        self.store = DatabaseStore(self.url)
        self.service = BudgetService(self.store.unit_of_work, self.clock)
        self.service.process_due(self.clock.now())
        self.service.add_budget(self.allocation.id, D(100000), currency="KRW", expected_revision=0, key="initial")
        self.assertEqual(self.store.snapshot(), before)

    def test_independent_connections_cannot_overallocate(self):
        second = DatabaseStore(self.url)
        def attempt(index):
            service = BudgetService((self.store if index == 0 else second).unit_of_work, self.clock)
            try:
                service.add_budget(self.allocation.id, D(600000), currency="KRW", expected_revision=0, key=str(index))
                return True
            except Conflict:
                return False
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sum(pool.map(attempt, range(2))), 1)
            self.assertEqual(self.store.snapshot().allocations[self.allocation.id].principal, D(600000))
        finally:
            second.close()

    def test_exception_rolls_back_balances_and_ledger(self):
        before = self.store.snapshot()
        with self.assertRaises(Conflict):
            self.service.add_budget(self.allocation.id, D(2000000), currency="KRW", expected_revision=0, key="fail")
        self.assertEqual(self.store.snapshot(), before)


@unittest.skipUnless(os.environ.get("QUANTRO_TEST_POSTGRES_URL"), "Dedicated PostgreSQL test URL is not configured")
class PostgreSQLTests(DatabaseTests):
    def setUp(self):
        from uuid import uuid4
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.schema import CreateSchema
        from quantro.infrastructure.database.schema import metadata, writer
        self.schema_name = "quantro_test_" + uuid4().hex
        self.admin = create_engine(os.environ["QUANTRO_TEST_POSTGRES_URL"])
        with self.admin.begin() as connection:
            connection.execute(CreateSchema(self.schema_name))
        self.url = str(make_url(os.environ["QUANTRO_TEST_POSTGRES_URL"]).update_query_dict(
            {"options": "-csearch_path=" + self.schema_name}).render_as_string(hide_password=False))
        self.store = DatabaseStore(self.url)
        metadata.create_all(self.store.engine)
        with self.store.engine.begin() as connection:
            connection.execute(writer.insert().values(id=1, revision=0))
        self.clock = Clock()
        self.service = BudgetService(self.store.unit_of_work, self.clock)
        self.service.create_account("account", D(1000000))
        self.service.register_instrument(Instrument("etf", "TEST", "KRX"))
        self.allocation = self.service.create_allocation("account", "etf", "Test")

    def tearDown(self):
        from sqlalchemy.schema import DropSchema
        self.store.close()
        with self.admin.begin() as connection:
            connection.execute(DropSchema(self.schema_name, cascade=True))
        self.admin.dispose()

    def test_roundtrip(self):
        self.assertEqual(self.store.snapshot().accounts["account"].unallocated_cash, D(1000000))
