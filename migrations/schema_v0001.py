from sqlalchemy import (JSON, BigInteger, CheckConstraint, Column, DateTime, ForeignKey,
                        Integer, MetaData, Numeric, String, Table, UniqueConstraint)

metadata = MetaData()
amount = Numeric(28, 8)
writer = Table("writer_lock", metadata, Column("id", Integer, primary_key=True),
               Column("revision", BigInteger, nullable=False))


def record_table(name, *columns, constraints=()):
    return Table(name, metadata, Column("id", String(128), primary_key=True),
                 Column("object_key", JSON, nullable=False), Column("payload", JSON, nullable=False),
                 *columns, *constraints)


accounts = record_table("accounts", Column("currency", String(3), nullable=False),
                        Column("gate_state", String(32), nullable=False), Column("revision", BigInteger, nullable=False))
instruments = record_table("instruments", Column("symbol", String(64), nullable=False),
    Column("exchange", String(32), nullable=False), constraints=(UniqueConstraint("symbol", "exchange"),))
allocations = record_table("allocations", Column("account_id", ForeignKey("accounts.id"), nullable=False),
    Column("instrument_id", ForeignKey("instruments.id"), nullable=False), Column("principal", amount, nullable=False),
    Column("revision", BigInteger, nullable=False),
    constraints=(UniqueConstraint("account_id", "instrument_id"), CheckConstraint("principal >= 0")))
schedules = record_table("schedule_versions", Column("allocation_id", ForeignKey("allocations.id"), nullable=False),
    Column("version", Integer, nullable=False), Column("amount", amount, nullable=False),
    constraints=(UniqueConstraint("allocation_id", "version"), CheckConstraint("amount > 0")))
occurrences = record_table("schedule_occurrences", Column("schedule_version_id", ForeignKey("schedule_versions.id"), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False), Column("status", String(32), nullable=False),
    constraints=(UniqueConstraint("schedule_version_id", "due_at"),))
assignments = record_table("strategy_assignments", Column("allocation_id", ForeignKey("allocations.id"), nullable=False),
    Column("active_priority", Integer), Column("revision", BigInteger, nullable=False),
    constraints=(UniqueConstraint("allocation_id", "active_priority"),))
history = record_table("strategy_assignment_revisions", Column("assignment_id", ForeignKey("strategy_assignments.id"), nullable=False),
    Column("revision", BigInteger, nullable=False), constraints=(UniqueConstraint("assignment_id", "revision"),))
evaluations = record_table("strategy_evaluations", Column("allocation_id", ForeignKey("allocations.id"), nullable=False),
    Column("market_event_id", String(128), nullable=False), constraints=(UniqueConstraint("allocation_id", "market_event_id"),))
orders = record_table("orders", Column("allocation_id", ForeignKey("allocations.id"), nullable=False),
    Column("client_order_id", String(128), unique=True, nullable=False), Column("state", String(32), nullable=False),
    Column("qty", BigInteger, nullable=False), Column("filled_qty", BigInteger, nullable=False),
    Column("price", amount, nullable=False),
    constraints=(CheckConstraint("qty > 0 AND filled_qty >= 0 AND filled_qty <= qty"),))
transactions = record_table("ledger_transactions", Column("reason", String(64), nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False))
fills = record_table("fills", Column("order_id", ForeignKey("orders.id"), nullable=False),
    Column("broker_id", String(64), nullable=False), Column("execution_id", String(256), nullable=False),
    constraints=(UniqueConstraint("broker_id", "execution_id"),))
outbox = record_table("outbox", Column("order_id", ForeignKey("orders.id"), nullable=False))
commands = record_table("idempotency_requests")
policies = record_table("risk_policies")
usage = record_table("daily_order_usage")
runs = record_table("simulation_runs", Column("status", String(32), nullable=False))
marks = record_table("market_marks")

cash_balances = Table("cash_balances", metadata, Column("id", String(160), primary_key=True),
    Column("account_id", ForeignKey("accounts.id"), nullable=False), Column("allocation_id", ForeignKey("allocations.id")),
    Column("currency", String(3), nullable=False), Column("total", amount, nullable=False),
    Column("reserved", amount, nullable=False), Column("unsettled", amount, nullable=False),
    CheckConstraint("total >= 0 AND reserved >= 0 AND unsettled >= 0 AND total >= reserved + unsettled"))
positions = Table("positions", metadata, Column("allocation_id", ForeignKey("allocations.id"), primary_key=True),
    Column("quantity", BigInteger, nullable=False), Column("reserved_quantity", BigInteger, nullable=False),
    Column("cost_basis", amount, nullable=False),
    CheckConstraint("quantity >= 0 AND reserved_quantity >= 0 AND quantity >= reserved_quantity AND cost_basis >= 0"))
reservations = Table("reservations", metadata, Column("order_id", ForeignKey("orders.id"), primary_key=True),
    Column("cash", amount, nullable=False), Column("quantity", BigInteger, nullable=False),
    CheckConstraint("cash >= 0 AND quantity >= 0"))
ledger_entries = Table("ledger_entries", metadata, Column("id", String(160), primary_key=True),
    Column("transaction_id", ForeignKey("ledger_transactions.id"), nullable=False),
    Column("account_id", ForeignKey("accounts.id"), nullable=False), Column("allocation_id", ForeignKey("allocations.id")),
    Column("bucket", String(64), nullable=False), Column("currency", String(3), nullable=False),
    Column("signed_amount", amount, nullable=False))

RECORDS = {
    "accounts": accounts, "instruments": instruments, "allocations": allocations,
    "schedules": schedules, "occurrences": occurrences, "assignments": assignments,
    "assignment_history": history, "evaluations": evaluations, "orders": orders,
    "transactions": transactions, "fills": fills, "outbox": outbox,
    "commands": commands, "risk_policies": policies, "daily_usage": usage,
    "simulation_runs": runs, "marks": marks,
}


def projection(field, value):
    if field == "accounts":
        return {"currency": value.currency, "gate_state": value.gate_state, "revision": value.revision}
    if field == "instruments":
        return {"symbol": value.symbol, "exchange": value.exchange}
    if field == "allocations":
        return {"account_id": value.account_id, "instrument_id": value.instrument_id,
                "principal": value.principal, "revision": value.revision}
    if field == "schedules":
        return {"allocation_id": value.allocation_id, "version": value.version, "amount": value.amount}
    if field == "occurrences":
        return {"schedule_version_id": value.schedule_version_id, "due_at": value.due_at, "status": value.status}
    if field == "assignments":
        return {"allocation_id": value.allocation_id,
                "active_priority": value.priority if value.enabled and not value.archived else None, "revision": value.revision}
    if field == "assignment_history":
        return {"assignment_id": value.id, "revision": value.revision}
    if field == "evaluations":
        return {"allocation_id": value.allocation_id, "market_event_id": value.market_event_id}
    if field == "orders":
        return {"allocation_id": value.allocation_id, "client_order_id": value.client_order_id,
                "state": value.state.value, "qty": value.quantity, "filled_qty": value.filled_quantity, "price": value.limit_price}
    if field == "transactions":
        return {"reason": value.reason, "effective_at": value.effective_at}
    if field == "fills":
        return {"order_id": value.fill.order_id, "broker_id": value.fill.broker_id, "execution_id": value.fill.execution_id}
    if field == "outbox":
        return {"order_id": value.order_id}
    if field == "simulation_runs":
        return {"status": value.status}
    return {}
