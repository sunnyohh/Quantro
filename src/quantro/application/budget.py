from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from quantro.domain.models import (Account, Allocation, Conflict, DomainError,
                                   Instrument, LedgerEntry, LedgerTransaction, aware, money)
from quantro.domain.schedule import BudgetScheduleVersion, Frequency, ScheduleOccurrence
from quantro.ports.clock import Clock
from quantro.ports.unit_of_work import UnitOfWork


class BudgetService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock,
                 id_factory: Callable[[], str] = lambda: str(uuid4())) -> None:
        self.uow_factory, self.clock, self.id_factory = uow_factory, clock, id_factory

    def create_account(self, account_id: str, initial_cash: Decimal, *, execution_mode: str = "PAPER") -> Account:
        money(initial_cash)
        with self.uow_factory() as uow:
            if account_id in uow.state.accounts:
                raise Conflict("Account already exists")
            account = Account(account_id, initial_cash, execution_mode=execution_mode)
            uow.state.accounts[account_id] = account
            self._ledger(uow, account_id, None, initial_cash, "INITIAL_DEPOSIT",
                         "external_clearing", "unallocated_cash", self.clock.now(), "setup", account_id)
            uow.commit()
            return deepcopy(account)

    def register_instrument(self, instrument: Instrument) -> None:
        with self.uow_factory() as uow:
            if instrument.id in uow.state.instruments or any(
                (i.symbol, i.exchange) == (instrument.symbol, instrument.exchange)
                for i in uow.state.instruments.values()
            ):
                raise Conflict("Instrument already exists")
            uow.state.instruments[instrument.id] = instrument
            uow.commit()

    def create_allocation(self, account_id: str, instrument_id: str, name: str) -> Allocation:
        with self.uow_factory() as uow:
            if account_id not in uow.state.accounts or instrument_id not in uow.state.instruments:
                raise DomainError("Unknown account or instrument")
            allocation = Allocation(self.id_factory(), account_id, instrument_id, name)
            uow.state.allocations[allocation.id] = allocation
            uow.commit()
            return deepcopy(allocation)

    def add_budget(self, allocation_id: str, amount: Decimal, *, currency: str,
                   expected_revision: int, key: str, actor: str = "user") -> LedgerTransaction:
        money(amount, positive=True)
        if currency != "KRW" or not key:
            raise DomainError("KRW and a nonempty idempotency key are required")
        scope = (actor, "budget/additions", allocation_id, key)
        body = (amount, currency, expected_revision)
        with self.uow_factory() as uow:
            if scope in uow.state.commands:
                old_body, result = uow.state.commands[scope]
                if old_body != body:
                    raise Conflict("Idempotency key reused with a different request")
                return deepcopy(result)
            allocation = uow.state.allocations[allocation_id]
            if allocation.revision != expected_revision:
                raise Conflict("Stale allocation revision")
            result = self._allocate(uow, allocation, amount, "BUDGET_ADDITION", self.clock.now(), actor, key)
            uow.state.commands[scope] = (body, result)
            uow.commit()
            return deepcopy(result)

    def deposit(self, account_id: str, amount: Decimal, *, key: str) -> LedgerTransaction:
        money(amount, positive=True)
        if not key:
            raise DomainError("Deposit requires an idempotency key")
        scope = ("deposit", account_id, key)
        with self.uow_factory() as uow:
            if scope in uow.state.commands:
                old_amount, result = uow.state.commands[scope]
                if old_amount != amount:
                    raise Conflict("Idempotency key reused with a different deposit")
                return deepcopy(result)
            account = uow.state.accounts[account_id]
            account.unallocated_cash += amount
            account.revision += 1
            result = self._ledger(uow, account_id, None, amount, "DEPOSIT", "external_clearing",
                                  "unallocated_cash", self.clock.now(), "user", key)
            uow.state.commands[scope] = (amount, result)
            uow.commit()
            return deepcopy(result)

    def set_schedule(self, allocation_id: str, amount: Decimal, frequency: Frequency,
                     *, expected_revision: int) -> BudgetScheduleVersion:
        money(amount, positive=True)
        frequency = Frequency(frequency)
        now = aware(self.clock.now()).astimezone(timezone.utc)
        with self.uow_factory() as uow:
            allocation = uow.state.allocations[allocation_id]
            if allocation.revision != expected_revision:
                raise Conflict("Stale allocation revision")
            # Process the old version through effective time before closing it.
            self._process(uow, now, allocation_id)
            previous = [s for s in uow.state.schedules.values() if s.allocation_id == allocation_id]
            for old in previous:
                if old.effective_to is None:
                    old.effective_to = now
                    old.enabled = False
            version = BudgetScheduleVersion(self.id_factory(), allocation_id,
                max((s.version for s in previous), default=0) + 1, amount, frequency, now, now)
            uow.state.schedules[version.id] = version
            allocation.revision += 1
            uow.commit()
            return deepcopy(version)

    def process_due(self, through: datetime) -> list[ScheduleOccurrence]:
        aware(through)
        with self.uow_factory() as uow:
            result = self._process(uow, through.astimezone(timezone.utc))
            uow.commit()
            return deepcopy(result)

    def pause_schedule(self, allocation_id: str, *, expected_revision: int) -> None:
        now = aware(self.clock.now()).astimezone(timezone.utc)
        with self.uow_factory() as uow:
            allocation = uow.state.allocations[allocation_id]
            if allocation.revision != expected_revision:
                raise Conflict("Stale allocation revision")
            self._process(uow, now, allocation_id)
            for schedule in uow.state.schedules.values():
                if schedule.allocation_id == allocation_id and schedule.effective_to is None:
                    schedule.effective_to = now
                    schedule.enabled = False
            allocation.revision += 1
            uow.commit()

    def _process(self, uow: UnitOfWork, through: datetime,
                 allocation_id: str | None = None) -> list[ScheduleOccurrence]:
        for schedule in uow.state.schedules.values():
            if allocation_id is not None and schedule.allocation_id != allocation_id:
                continue
            index = 1
            while True:
                due = schedule.due_at(index)
                if due > through or (schedule.effective_to is not None and due > schedule.effective_to):
                    break
                key = (schedule.id, due)
                if key not in uow.state.occurrences:
                    uow.state.occurrences[key] = ScheduleOccurrence(schedule.id, due, schedule.amount)
                index += 1
        pending = sorted((o for o in uow.state.occurrences.values()
            if o.status == "PENDING_FUNDS" and o.due_at <= through
            and (allocation_id is None or uow.state.schedules[o.schedule_version_id].allocation_id == allocation_id)),
            key=lambda o: (o.due_at, o.schedule_version_id))
        blocked_accounts = set()
        for occurrence in pending:
            schedule = uow.state.schedules[occurrence.schedule_version_id]
            allocation = uow.state.allocations[schedule.allocation_id]
            account = uow.state.accounts[allocation.account_id]
            if account.id in blocked_accounts or account.unallocated_cash < occurrence.amount:
                blocked_accounts.add(account.id)
                continue
            transaction = self._allocate(uow, allocation, occurrence.amount, "SCHEDULE_ALLOCATION",
                occurrence.due_at, "scheduler", schedule.id)
            occurrence.status, occurrence.transaction_id = "APPLIED", transaction.id
        return pending

    def _allocate(self, uow: UnitOfWork, allocation: Allocation, amount: Decimal,
                  reason: str, effective_at: datetime, actor: str, correlation_id: str) -> LedgerTransaction:
        account = uow.state.accounts[allocation.account_id]
        if account.unallocated_cash < amount:
            raise Conflict("Insufficient unallocated cash")
        account.unallocated_cash -= amount
        account.revision += 1
        allocation.principal += amount
        allocation.cash_total += amount
        allocation.revision += 1
        return self._ledger(uow, account.id, allocation.id, amount, reason, "unallocated_cash",
                            "allocation_cash", effective_at, actor, correlation_id)

    def _ledger(self, uow: UnitOfWork, account_id: str, allocation_id: str | None,
                amount: Decimal, reason: str, source: str, destination: str,
                effective_at: datetime, actor: str, correlation_id: str) -> LedgerTransaction:
        transaction = LedgerTransaction(self.id_factory(), reason,
            aware(effective_at).astimezone(timezone.utc),
            aware(self.clock.now()).astimezone(timezone.utc), actor, correlation_id,
            (LedgerEntry(account_id, None, source, -amount),
             LedgerEntry(account_id, allocation_id, destination, amount)))
        uow.state.transactions.append(transaction)
        return transaction
