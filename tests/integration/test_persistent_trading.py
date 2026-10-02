import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quantro.infrastructure.database.store import DatabaseStore
from tests.unit import test_trading


class PersistentTradingTests(test_trading.TradingTests):
    def make_store(self):
        self.temp = TemporaryDirectory()
        store = DatabaseStore("sqlite:///" + (Path(self.temp.name) / "trading.db").as_posix())
        store.initialize_local()
        return store

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_unknown_order_restarts_without_losing_reservations(self):
        from quantro.application.trading import TradingService
        from quantro.domain.trading import OrderState
        from quantro.strategies.examples import default_registry
        evaluation = self.evaluate()
        # The process disappeared after storing SUBMITTING and before SDK result handling.
        with self.store.unit_of_work() as uow:
            uow.state.orders[evaluation.order_id].transition(OrderState.SUBMITTING)
            uow.commit()
        url = self.store.engine.url.render_as_string(hide_password=False)
        self.store.close()
        self.store = DatabaseStore(url)
        self.trading = TradingService(self.store.unit_of_work, self.clock, default_registry())
        self.assertFalse(self.trading.dispatch(evaluation.order_id, self.broker, lease_token="worker"))
        self.assertFalse(self.trading.recover(evaluation.order_id, self.broker))
        state = self.store.snapshot()
        self.assertEqual(state.orders[evaluation.order_id].state, OrderState.UNKNOWN)
        self.assertEqual(state.allocations[self.allocation.id].cash_reserved, 100000)


@unittest.skipUnless(os.environ.get("QUANTRO_TEST_POSTGRES_URL"), "Dedicated PostgreSQL test URL is not configured")
class PostgreSQLTradingTests(PersistentTradingTests):
    def make_store(self):
        from uuid import uuid4
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.schema import CreateSchema
        from quantro.infrastructure.database.schema import metadata, writer
        self.schema_name = "quantro_test_" + uuid4().hex
        self.admin = create_engine(os.environ["QUANTRO_TEST_POSTGRES_URL"])
        with self.admin.begin() as connection:
            connection.execute(CreateSchema(self.schema_name))
        url = make_url(os.environ["QUANTRO_TEST_POSTGRES_URL"]).update_query_dict(
            {"options": "-csearch_path=" + self.schema_name}).render_as_string(hide_password=False)
        store = DatabaseStore(url)
        metadata.create_all(store.engine)
        with store.engine.begin() as connection:
            connection.execute(writer.insert().values(id=1, revision=0))
        return store

    def tearDown(self):
        from sqlalchemy.schema import DropSchema
        self.store.close()
        with self.admin.begin() as connection:
            connection.execute(DropSchema(self.schema_name, cascade=True))
        self.admin.dispose()
