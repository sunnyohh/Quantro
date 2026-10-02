from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import hmac
import json
import os
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from quantro.application.budget import BudgetService
from quantro.application.simulation import SimulationService
from quantro.application.trading import TradingService
from quantro.domain.models import Conflict, DomainError, Instrument, aware
from quantro.domain.schedule import Frequency
from quantro.domain.trading import MarketBar, RiskPolicy
from quantro.infrastructure.clock import SystemClock
from quantro.infrastructure.database.store import DatabaseStore
from quantro.simulation.engine import DepositEvent, RunConfig
from quantro.strategies.examples import default_registry


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def decimal(value: str):
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise DomainError("Invalid decimal string") from exc


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AccountInput(Input):
    initial_cash: str
    execution_mode: Literal["PAPER", "BACKTEST"] = "PAPER"


class InstrumentInput(Input):
    symbol: str
    exchange: str = "KRX"
    lot_size: int = 1


class AllocationInput(Input):
    instrument_id: str
    name: str


class BudgetInput(Input):
    amount: str
    currency: Literal["KRW"] = "KRW"
    expected_revision: int


class DepositInput(Input):
    amount: str


class ScheduleInput(BudgetInput):
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY"]
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    change_policy: Literal["RESET_FROM_EFFECTIVE_TIME"] = "RESET_FROM_EFFECTIVE_TIME"


class RevisionInput(Input):
    expected_revision: int


class AssignmentInput(Input):
    plugin_id: str
    plugin_version: str
    config: dict
    priority: int


class AssignmentUpdate(Input):
    config: dict
    priority: int
    enabled: bool
    expected_revision: int


class BarInput(Input):
    id: str
    start_at: str
    end_at: str
    available_at: str
    open: str
    high: str
    low: str
    close: str
    volume: int
    trading: bool = True


class RiskInput(Input):
    version: str
    max_order_amount: str
    max_daily_amount: str
    max_quote_age_seconds: int
    fee_rate: str


class DepositEventInput(Input):
    at: str
    amount: str


class RunInput(Input):
    instrument_id: str
    start_at: str
    end_at: str
    initial_cash: str
    initial_budget: str
    plugin_id: str
    plugin_version: str
    config: dict
    risk: RiskInput
    dataset_id: str
    code_version: str
    participation: str
    slippage_rate: str
    sell_tax_rate: str
    bars: list[BarInput]
    deposits: list[DepositEventInput] = []
    schedule_amount: str | None = None
    schedule_frequency: Literal["DAILY", "WEEKLY", "MONTHLY"] | None = None
    settlement_days: int = 0


class BorrowedUnitOfWork:
    """Compose application mutations into the HTTP idempotency transaction."""
    def __init__(self, parent):
        self.state = parent.state
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def commit(self):
        self.state.validate()
    def rollback(self):
        raise RuntimeError("Outer HTTP transaction must be rolled back")


def create_app(store, *, token: str, clock=None) -> FastAPI:
    if len(token) < 24:
        raise ValueError("Configure QUANTRO_API_TOKEN with at least 24 characters")
    clock = clock or SystemClock()
    registry = default_registry()
    bearer = HTTPBearer(auto_error=False)
    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    app = FastAPI(title="Quantro", version="0.1.0", dependencies=[Depends(authenticate)],
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    def error(status, code, message, request, details=None, headers=None):
        return JSONResponse(status_code=status, content={"code": code, "message": message,
            "details": details or {}, "correlation_id": request.state.correlation_id}, headers=headers)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return error(409, "CONFLICT", str(exc), request)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error(422, "INVALID_REQUEST", str(exc), request)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return error(404, "NOT_FOUND", "Resource was not found", request)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return error(422, "INVALID_SCHEMA", "Request schema validation failed", request,
                     {"fields": [".".join(map(str, item["loc"])) for item in exc.errors()]})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, "HTTP_ERROR", str(exc.detail), request, headers=exc.headers)

    def services(uow):
        factory = lambda: BorrowedUnitOfWork(uow)
        return BudgetService(factory, clock), TradingService(factory, clock, registry), SimulationService(factory, clock)

    def mutate(request: Request, body: Input | None, work):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 256:
            raise DomainError("A nonempty Idempotency-Key of at most 256 characters is required")
        payload = body.model_dump(mode="json") if body else {}
        body_hash = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        scope = ("http", "owner", request.method, request.url.path, key)
        with store.unit_of_work() as uow:
            if scope in uow.state.commands:
                old_hash, response = uow.state.commands[scope]
                if body_hash != old_hash:
                    raise Conflict("Idempotency key reused with a different request")
                return response
            result = json_value(work(uow, *services(uow)))
            uow.state.commands[scope] = (body_hash, result)
            uow.commit()
            return result

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "live_enabled": False}

    @app.get("/api/v1/openapi.json")
    def openapi():
        return app.openapi()

    @app.get("/api/v1/accounts")
    def accounts():
        return json_value(list(store.snapshot().accounts.values()))

    @app.post("/api/v1/accounts", status_code=201)
    def create_account(body: AccountInput, request: Request):
        return mutate(request, body, lambda u, b, t, s: b.create_account(str(uuid4()), decimal(body.initial_cash),
                                                                       execution_mode=body.execution_mode))

    @app.post("/api/v1/accounts/{account_id}/deposits", status_code=201)
    def deposit(account_id: str, body: DepositInput, request: Request):
        return mutate(request, body, lambda u, b, t, s: b.deposit(account_id, decimal(body.amount),
                                                                key=request.headers["Idempotency-Key"]))

    @app.get("/api/v1/instruments")
    def instruments():
        return json_value(list(store.snapshot().instruments.values()))

    @app.post("/api/v1/instruments", status_code=201)
    def create_instrument(body: InstrumentInput, request: Request):
        def work(u, b, t, s):
            instrument = Instrument(str(uuid4()), body.symbol, body.exchange, lot_size=body.lot_size)
            b.register_instrument(instrument)
            return instrument
        return mutate(request, body, work)

    @app.post("/api/v1/accounts/{account_id}/allocations", status_code=201)
    def create_allocation(account_id: str, body: AllocationInput, request: Request):
        return mutate(request, body, lambda u, b, t, s: b.create_allocation(account_id, body.instrument_id, body.name))

    @app.get("/api/v1/allocations/{allocation_id}")
    def allocation(allocation_id: str):
        state = store.snapshot()
        value = state.allocations[allocation_id]
        result = json_value(value)
        result.update(cash_available=str(value.cash_available), quantity_available=value.quantity_available,
            schedules=json_value([s for s in state.schedules.values() if s.allocation_id == allocation_id]),
            strategies=json_value([a for a in state.assignments.values() if a.allocation_id == allocation_id]),
            pending_budget=str(sum((o.amount for o in state.occurrences.values()
                if o.status == "PENDING_FUNDS" and state.schedules[o.schedule_version_id].allocation_id == allocation_id), Decimal(0))))
        return result

    @app.post("/api/v1/allocations/{allocation_id}/budget/additions", status_code=201)
    def add_budget(allocation_id: str, body: BudgetInput, request: Request):
        def work(u, b, t, s):
            transaction = b.add_budget(allocation_id, decimal(body.amount), currency=body.currency,
                expected_revision=body.expected_revision, key=request.headers["Idempotency-Key"])
            allocation = u.state.allocations[allocation_id]
            return {"transaction_id": transaction.id, "revision": allocation.revision,
                    "principal": allocation.principal, "cash_total": allocation.cash_total}
        return mutate(request, body, work)

    @app.get("/api/v1/allocations/{allocation_id}/budget/history")
    def history(allocation_id: str):
        state = store.snapshot()
        state.allocations[allocation_id]
        return {"transactions": json_value([tx for tx in state.transactions if any(e.allocation_id == allocation_id for e in tx.entries)]),
                "occurrences": json_value([o for o in state.occurrences.values()
                    if state.schedules[o.schedule_version_id].allocation_id == allocation_id])}

    @app.post("/api/v1/allocations/{allocation_id}/schedule", status_code=201)
    @app.patch("/api/v1/allocations/{allocation_id}/schedule")
    def schedule(allocation_id: str, body: ScheduleInput, request: Request):
        def work(u, b, t, s):
            active = [v for v in u.state.schedules.values() if v.allocation_id == allocation_id and v.effective_to is None]
            if request.method == "POST" and active:
                raise Conflict("Schedule already exists; use PATCH")
            if request.method == "PATCH" and not active:
                raise Conflict("Create a schedule first")
            version = b.set_schedule(allocation_id, decimal(body.amount), Frequency(body.frequency), expected_revision=body.expected_revision)
            return {"schedule": version, "next_due_at": version.due_at(1),
                    "revision": u.state.allocations[allocation_id].revision, "principal": u.state.allocations[allocation_id].principal}
        return mutate(request, body, work)

    @app.post("/api/v1/allocations/{allocation_id}/schedule/pause")
    def pause(allocation_id: str, body: RevisionInput, request: Request):
        def work(u, b, t, s):
            b.pause_schedule(allocation_id, expected_revision=body.expected_revision)
            return {"revision": u.state.allocations[allocation_id].revision, "status": "PAUSED"}
        return mutate(request, body, work)

    @app.get("/api/v1/strategy-plugins")
    def plugins():
        return registry.descriptors()

    @app.post("/api/v1/allocations/{allocation_id}/strategy-assignments", status_code=201)
    def assign(allocation_id: str, body: AssignmentInput, request: Request):
        return mutate(request, body, lambda u, b, t, s: t.assign(allocation_id, body.plugin_id, body.plugin_version, body.config, body.priority))

    @app.patch("/api/v1/strategy-assignments/{assignment_id}")
    def edit_assignment(assignment_id: str, body: AssignmentUpdate, request: Request):
        return mutate(request, body, lambda u, b, t, s: t.update_assignment(assignment_id, **body.model_dump()))

    @app.delete("/api/v1/strategy-assignments/{assignment_id}")
    def archive_assignment(assignment_id: str, body: RevisionInput, request: Request):
        def work(u, b, t, s):
            old = u.state.assignments[assignment_id]
            return t.update_assignment(assignment_id, config=dict(old.config), priority=old.priority,
                enabled=False, archived=True, expected_revision=body.expected_revision)
        return mutate(request, body, work)

    @app.post("/api/v1/accounts/{account_id}/execution/stop")
    def stop(account_id: str, request: Request):
        def work(u, b, t, s):
            t.stop(account_id)
            return u.state.accounts[account_id]
        return mutate(request, None, work)

    @app.get("/api/v1/accounts/{account_id}/dashboard")
    def dashboard(account_id: str):
        state = store.snapshot()
        account = state.accounts[account_id]
        allocations = [a for a in state.allocations.values() if a.account_id == account_id]
        budget_pool = account.unallocated_cash + sum((a.principal for a in allocations), Decimal(0))
        def nav(a):
            mark = state.marks.get(a.instrument_id)
            return a.nav(mark[0]) if mark else (a.cash_total if not a.quantity else None)
        values = [nav(a) for a in allocations]
        total_nav = None if any(v is None for v in values) else account.unallocated_cash + sum(values, Decimal(0))
        return json_value({"account": account, "account_nav": total_nav, "valuation_complete": total_nav is not None,
            "principal_pool": budget_pool, "unallocated_cash": account.unallocated_cash,
            "allocated_principal": sum((a.principal for a in allocations), Decimal(0)),
            "reserved_cash": sum((a.cash_reserved for a in allocations), Decimal(0)),
            "budget_distribution": [{"id": a.id, "name": a.name, "amount": a.principal} for a in allocations]
                + [{"id": "unallocated", "name": "미할당", "amount": account.unallocated_cash}],
            "asset_distribution": [{"id": a.id, "name": a.name, "amount": nav(a)} for a in allocations]
                + [{"id": "unallocated", "name": "미할당", "amount": account.unallocated_cash}],
            "allocations": allocations})

    @app.get("/api/v1/orders")
    def orders(cursor: str | None = None, limit: int = 50):
        if not 1 <= limit <= 200:
            raise DomainError("Order page limit must be between 1 and 200")
        values = sorted(store.snapshot().orders.values(), key=lambda o: (o.created_at, o.id))
        if cursor is not None:
            indices = [i for i, o in enumerate(values) if o.id == cursor]
            if not indices:
                raise DomainError("Unknown order cursor")
            values = values[indices[0] + 1:]
        page = values[:limit]
        return {"items": json_value(page), "next_cursor": page[-1].id if len(values) > limit else None}

    @app.post("/api/v1/simulation-runs", status_code=202)
    def create_run(body: RunInput, request: Request):
        def work(u, b, t, s):
            instrument = u.state.instruments[body.instrument_id]
            def timestamp(text):
                try:
                    return aware(datetime.fromisoformat(text))
                except ValueError as exc:
                    raise DomainError("Timestamp must be offset-aware ISO 8601") from exc
            bars = tuple(MarketBar(v.id, instrument.id, timestamp(v.start_at), timestamp(v.end_at), timestamp(v.available_at),
                decimal(v.open), decimal(v.high), decimal(v.low), decimal(v.close), v.volume, v.trading) for v in body.bars)
            risk = RiskPolicy(body.risk.version, decimal(body.risk.max_order_amount), decimal(body.risk.max_daily_amount),
                              body.risk.max_quote_age_seconds, decimal(body.risk.fee_rate))
            config = RunConfig(timestamp(body.start_at), timestamp(body.end_at), decimal(body.initial_cash), decimal(body.initial_budget),
                instrument, body.plugin_id, body.plugin_version, tuple(sorted(body.config.items())), risk, body.dataset_id, body.code_version,
                decimal(body.participation), decimal(body.slippage_rate), decimal(body.sell_tax_rate),
                tuple(DepositEvent(timestamp(e.at), decimal(e.amount)) for e in body.deposits),
                decimal(body.schedule_amount) if body.schedule_amount is not None else None,
                Frequency(body.schedule_frequency) if body.schedule_frequency is not None else None, body.settlement_days)
            run = s.enqueue(config, bars)
            return {"id": run.id, "status": run.status}
        return mutate(request, body, work)

    @app.get("/api/v1/simulation-runs/{run_id}")
    def simulation(run_id: str):
        return json_value(store.snapshot().simulation_runs[run_id])

    @app.post("/api/v1/simulation-runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str, request: Request):
        return mutate(request, None, lambda u, b, t, s: s.cancel(run_id))

    @app.get("/api/v1/simulation-comparisons")
    def comparison(run_ids: str):
        state = store.snapshot()
        ids = run_ids.split(",")
        if not 2 <= len(ids) <= 10:
            raise DomainError("Compare 2 to 10 completed runs")
        runs = [state.simulation_runs[run_id] for run_id in ids]
        if any(run.status != "COMPLETED" or run.result is None for run in runs):
            raise Conflict("Only completed runs can be compared")
        manifests = [json.loads(run.result.manifest_json) for run in runs]
        dimensions = ("start_at", "end_at", "initial_cash", "deposits", "dataset_id")
        differences = {key: [m["config"][key] for m in manifests] for key in dimensions
                       if any(m["config"][key] != manifests[0]["config"][key] for m in manifests[1:])}
        if any(m["data_hash"] != manifests[0]["data_hash"] for m in manifests[1:]):
            differences["data_hash"] = [m["data_hash"] for m in manifests]
        return json_value({"same_comparison_group": not differences, "manifest_differences": differences,
                           "runs": [{"id": run.id, "metrics": run.result.metrics, "valuations": run.result.valuations} for run in runs]})

    return app


def app_factory():
    token = os.environ.get("QUANTRO_API_TOKEN", "")
    url = os.environ.get("QUANTRO_DATABASE_URL", "")
    if not url:
        raise RuntimeError("Configure QUANTRO_DATABASE_URL")
    store = DatabaseStore(url)
    if url.startswith("sqlite:///"):
        store.initialize_local()
    return create_app(store, token=token)
