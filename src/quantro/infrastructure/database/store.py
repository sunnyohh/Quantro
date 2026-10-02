from copy import deepcopy
from hashlib import sha256
import json

from sqlalchemy import create_engine, event, insert, select, update
from sqlalchemy.exc import IntegrityError

from quantro.domain.models import Conflict, DomainError, State
from .codec import decode, encode
from .schema import (RECORDS, cash_balances, ledger_entries, metadata, positions,
                     projection, reservations, writer)


def row_id(key):
    if isinstance(key, str):
        return key
    return sha256(json.dumps(encode(key), sort_keys=True).encode()).hexdigest()


class DatabaseStore:
    """Durable PAPER/BACKTEST adapter. A database writer row serializes all writers.

    Rows have indexed relational projections and versioned typed JSON snapshots.
    SQLite is supported for local validation; PostgreSQL is the deployment backend.
    Separate databases isolate simulation runs from the operational ledger.
    """

    def __init__(self, url: str):
        if not (url.startswith("sqlite:///") or url.startswith("postgresql+psycopg://")):
            raise DomainError("Use SQLite locally or PostgreSQL with psycopg")
        self.engine = create_engine(url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")

    def initialize_local(self):
        if self.engine.dialect.name != "sqlite":
            raise DomainError("Use Alembic migrations to initialize PostgreSQL")
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            if connection.execute(select(writer.c.id)).first() is None:
                connection.execute(insert(writer).values(id=1, revision=0))

    def unit_of_work(self):
        return DatabaseUnitOfWork(self)

    def snapshot(self):
        with self.unit_of_work() as uow:
            return deepcopy(uow.state)

    def close(self):
        self.engine.dispose()


class DatabaseUnitOfWork:
    def __init__(self, store):
        self.store = store
        self.finished = False

    def __enter__(self):
        self.connection = self.store.engine.connect()
        try:
            if self.store.engine.dialect.name == "sqlite":
                self.connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                self.connection.begin()
            self.connection.execute(select(writer.c.id).where(writer.c.id == 1).with_for_update()).one()
            self.state = State()
            for field, table in RECORDS.items():
                records = self.connection.execute(select(table).order_by(table.c.id)).mappings()
                values = [(decode(row["object_key"]), decode(row["payload"])) for row in records]
                if field == "transactions":
                    values.sort(key=lambda pair: pair[0])
                    setattr(self.state, field, [v for _, v in values])
                else:
                    setattr(self.state, field, dict(values))
            self.state.validate()
            self.original = deepcopy(self.state)
            return self
        except Exception:
            self.connection.rollback()
            self.connection.close()
            raise

    def commit(self):
        if self.finished:
            raise DomainError("UnitOfWork is already finished")
        self.state.validate()
        immutable = {"transactions", "commands", "evaluations", "assignment_history", "instruments"}
        try:
            for field, table in RECORDS.items():
                current, previous = getattr(self.state, field), getattr(self.original, field)
                if field == "transactions":
                    current = {str(i).zfill(20): value for i, value in enumerate(current)}
                    previous = {str(i).zfill(20): value for i, value in enumerate(previous)}
                if not previous.keys() <= current.keys():
                    raise DomainError("Financial records cannot be hard deleted")
                for key, value in current.items():
                    if key in previous and value == previous[key]:
                        continue
                    if key in previous and field in immutable:
                        raise DomainError("Persisted audit/version records are immutable")
                    if field == "fills" and key in previous and value.fill != previous[key].fill:
                        raise DomainError("Persisted execution is immutable")
                    if field == "simulation_runs" and key in previous:
                        old = previous[key]
                        if value.config != old.config or value.bars != old.bars:
                            raise DomainError("Simulation inputs are immutable")
                    record_id = value.id if field == "transactions" else row_id(key)
                    values = {"id": record_id, "object_key": encode(key), "payload": encode(value), **projection(field, value)}
                    self._save(table, record_id, values, existing=key in previous)
                    if field == "transactions":
                        for index, entry in enumerate(value.entries):
                            self.connection.execute(insert(ledger_entries).values(
                                id=f"{value.id}:{index}", transaction_id=value.id, account_id=entry.account_id,
                                allocation_id=entry.allocation_id, bucket=entry.bucket,
                                currency=entry.currency, signed_amount=entry.signed_amount))
            self._balances()
            self.connection.execute(update(writer).where(writer.c.id == 1).values(revision=writer.c.revision + 1))
            self.connection.commit()
            self.finished = True
        except IntegrityError as exc:
            self.connection.rollback()
            self.finished = True
            raise Conflict("Database uniqueness or balance constraint failed") from exc
        except Exception:
            self.connection.rollback()
            self.finished = True
            raise

    def _save(self, table, key, values, *, existing=None, key_column="id"):
        column = table.c[key_column]
        if existing is None:
            existing = self.connection.execute(select(column).where(column == key)).first() is not None
        if existing:
            self.connection.execute(update(table).where(column == key).values(**values))
        else:
            self.connection.execute(insert(table).values(**values))

    def _balances(self):
        for account in self.state.accounts.values():
            key = f"account:{account.id}"
            self._save(cash_balances, key, dict(id=key, account_id=account.id, allocation_id=None,
                currency=account.currency, total=account.unallocated_cash, reserved=0, unsettled=0))
        for allocation in self.state.allocations.values():
            key = f"allocation:{allocation.id}"
            self._save(cash_balances, key, dict(id=key, account_id=allocation.account_id, allocation_id=allocation.id,
                currency="KRW", total=allocation.cash_total, reserved=allocation.cash_reserved, unsettled=allocation.cash_unsettled))
            self._save(positions, allocation.id, dict(allocation_id=allocation.id, quantity=allocation.quantity,
                reserved_quantity=allocation.quantity_reserved, cost_basis=allocation.position_cost), key_column="allocation_id")
        for order in self.state.orders.values():
            self._save(reservations, order.id, dict(order_id=order.id, cash=order.reserved_cash,
                       quantity=order.reserved_quantity), key_column="order_id")

    def rollback(self):
        self.connection.rollback()
        self.finished = True

    def __exit__(self, exc_type, exc_value, traceback):
        if not self.finished:
            self.connection.rollback()
        self.connection.close()
