from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from quantro.application.budget import BudgetService
from quantro.application.trading import TradingService
from quantro.domain.models import DomainError, Instrument, aware, money
from quantro.domain.schedule import Frequency
from quantro.domain.trading import MarketBar, Quote, RiskPolicy
from quantro.infrastructure.memory import MemoryStore
from quantro.ports.broker import AccountSnapshot
from quantro.strategies.examples import default_registry
from .simulated_broker import SimulatedBroker


def canonical(value) -> str:
    return json.dumps(value, default=lambda v: v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else str(v),
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class DepositEvent:
    at: datetime
    amount: Decimal

    def __post_init__(self):
        aware(self.at)
        money(self.amount, positive=True)


@dataclass(frozen=True)
class RunConfig:
    start_at: datetime
    end_at: datetime
    initial_cash: Decimal
    initial_budget: Decimal
    instrument: Instrument
    plugin_id: str
    plugin_version: str
    strategy_config: tuple[tuple[str, str | int | bool], ...]
    risk: RiskPolicy
    dataset_id: str
    code_version: str
    participation: Decimal
    slippage_rate: Decimal
    sell_tax_rate: Decimal
    deposits: tuple[DepositEvent, ...] = ()
    schedule_amount: Decimal | None = None
    schedule_frequency: Frequency | None = None
    settlement_days: int = 0

    def __post_init__(self):
        aware(self.start_at)
        aware(self.end_at)
        money(self.initial_cash)
        money(self.initial_budget)
        if self.start_at >= self.end_at or self.initial_budget > self.initial_cash:
            raise DomainError("Invalid run period or initial budget")
        if not self.dataset_id or not self.code_version:
            raise DomainError("Run requires dataset and code versions")
        if (self.schedule_amount is None) != (self.schedule_frequency is None):
            raise DomainError("Schedule amount and frequency must be provided together")
        if any(not self.start_at <= event.at < self.end_at for event in self.deposits):
            raise DomainError("Deposit falls outside the run period")


@dataclass(frozen=True)
class Valuation:
    at: datetime
    nav: Decimal
    unitized_nav: Decimal
    contributions: Decimal
    pending_budget: Decimal


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    manifest_json: str
    metrics: dict[str, str | int]
    valuations: tuple[Valuation, ...]
    trades_json: str
    checksum: str


class SimulationClock:
    def __init__(self, time: datetime):
        self.time = time

    def now(self):
        return self.time


class BacktestEngine:
    def run(self, config: RunConfig, bars: tuple[MarketBar, ...], *, cancel_requested=lambda: False,
            on_progress=lambda count, time: None) -> RunResult:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            return self._run(config, bars, cancel_requested=cancel_requested, on_progress=on_progress)

    def _run(self, config, bars, *, cancel_requested, on_progress):
        if any(b.instrument_id != config.instrument.id for b in bars):
            raise DomainError("Dataset contains a different instrument")
        if len({b.id for b in bars}) != len(bars):
            raise DomainError("Duplicate market event ids")
        ordered = tuple(sorted(bars, key=lambda b: (b.available_at, b.instrument_id, b.id)))
        if any(ordered[i].available_at >= ordered[i + 1].available_at for i in range(len(ordered) - 1)):
            raise DomainError("Daily dataset has duplicate publication times")
        data_hash = sha256(canonical([asdict(b) for b in ordered]).encode()).hexdigest()
        from pathlib import Path
        package_root = Path(__file__).resolve().parents[1]
        code_digest = sha256()
        for path in sorted(package_root.rglob("*.py")):
            code_digest.update(path.relative_to(package_root).as_posix().encode())
            code_digest.update(path.read_text(encoding="utf-8").encode())
        manifest = {"config": asdict(config), "data_hash": data_hash,
                    "engine_code_hash": code_digest.hexdigest(),
                    "fill_model": "next-raw-daily-bar-limit-v1", "seed": 0,
                    "assumptions": ["Fixed instrument universe; possible survivorship bias",
                        "Input OHLC must be unadjusted; no corporate-action model yet",
                        "Publication times are caller supplied; point-in-time revisions not inferred",
                        "Calendar-day settlement delay; no holiday settlement calendar",
                        "One daily evaluation; price tick is one KRW; intrabar path/queue not modeled"]}
        manifest_json = canonical(manifest)
        run_id = sha256(manifest_json.encode()).hexdigest()
        counter = 0
        def next_id():
            nonlocal counter
            counter += 1
            return str(uuid5(NAMESPACE_URL, f"quantro:{run_id}:{counter}"))
        store, clock = MemoryStore(), SimulationClock(config.start_at)
        budget = BudgetService(store.unit_of_work, clock, next_id)
        trading = TradingService(store.unit_of_work, clock, default_registry(), next_id)
        budget.create_account("run", config.initial_cash, execution_mode="BACKTEST")
        budget.register_instrument(config.instrument)
        allocation = budget.create_allocation("run", config.instrument.id, "Backtest")
        if config.initial_budget:
            budget.add_budget(allocation.id, config.initial_budget, currency="KRW", expected_revision=0, key="initial")
        if config.schedule_amount is not None:
            revision = store.snapshot().allocations[allocation.id].revision
            budget.set_schedule(allocation.id, config.schedule_amount, config.schedule_frequency, expected_revision=revision)
        trading.assign(allocation.id, config.plugin_id, config.plugin_version, dict(config.strategy_config), 0)
        trading.configure_risk("run", config.risk)
        broker = SimulatedBroker(participation=config.participation, slippage_rate=config.slippage_rate,
            sell_tax_rate=config.sell_tax_rate, settlement_days=config.settlement_days,
            lot_sizes={config.instrument.id: config.instrument.lot_size})
        deposits = sorted(enumerate(config.deposits), key=lambda pair: (pair[1].at, pair[0]))
        deposit_index, contributions = 0, Decimal(0)
        valuations = []
        previous_nav, unit, peak, max_drawdown = config.initial_cash, Decimal(1), Decimal(1), Decimal(0)
        window = []
        status = "COMPLETED"
        for bar in ordered:
            if bar.available_at < config.start_at:
                window.append(bar)
                continue
            if bar.available_at >= config.end_at:
                break
            if cancel_requested():
                status = "CANCELED"
                break
            clock.time = bar.available_at
            period_deposits = Decimal(0)
            while deposit_index < len(deposits) and deposits[deposit_index][1].at <= clock.now():
                index, event = deposits[deposit_index]
                # Preserve the actual event time, not the next publication time.
                clock.time = event.at
                budget.deposit("run", event.amount, key=f"deposit:{index}")
                clock.time = bar.available_at
                period_deposits += event.amount
                deposit_index += 1
            contributions += period_deposits
            trading.settle(clock.now())
            budget.process_due(clock.now())
            for snapshot in broker.on_bar(bar):
                trading.recover(snapshot.order_id, broker)
            window.append(bar)
            trading.start("run", broker, lease_token="replay", lease_seconds=60)
            state = store.snapshot()
            raw_cash = state.accounts["run"].unallocated_cash + sum((a.cash_total - a.cash_unsettled
                       for a in state.allocations.values()), Decimal(0))
            evaluation = trading.evaluate(allocation.id, bar.id, tuple(window),
                Quote(bar.instrument_id, bar.close, bar.close, bar.available_at, bar.trading, bar.trading),
                AccountSnapshot("run", raw_cash, clock.now()), lease_token="replay")
            if evaluation.order_id:
                trading.dispatch(evaluation.order_id, broker, lease_token="replay")
            state = store.snapshot()
            nav = state.accounts["run"].unallocated_cash + state.allocations[allocation.id].nav(bar.close)
            base = previous_nav + period_deposits
            if base:
                unit *= nav / base
            previous_nav = nav
            peak = max(peak, unit)
            max_drawdown = max(max_drawdown, (peak - unit) / peak)
            pending = sum((o.amount for o in state.occurrences.values() if o.status == "PENDING_FUNDS"), Decimal(0))
            valuations.append(Valuation(clock.now(), nav, unit, contributions, pending))
            on_progress(len(valuations), bar.id)
        # Apply trailing deposits and budgets in [start, end) even with no final bar.
        from datetime import timedelta
        through = config.end_at - timedelta(microseconds=1)
        if status == "COMPLETED":
            while deposit_index < len(deposits):
                index, event = deposits[deposit_index]
                clock.time = event.at
                budget.deposit("run", event.amount, key=f"deposit:{index}")
                contributions += event.amount
                deposit_index += 1
            clock.time = through
            trading.settle(through)
            budget.process_due(through)
        state = store.snapshot()
        final_price = window[-1].close if window else Decimal(0)
        final_nav = state.accounts["run"].unallocated_cash + state.allocations[allocation.id].nav(final_price)
        trades_json = canonical([asdict(record.fill) for record in state.fills.values()])
        metrics = {"initial_nav": str(config.initial_cash), "net_contributions": str(contributions),
            "final_nav": str(final_nav), "profit": str(final_nav - config.initial_cash - contributions),
            "twr": str(unit - 1), "mdd": str(max_drawdown), "order_count": len(state.orders),
            "fill_count": len(state.fills), "allocated_principal": str(state.allocations[allocation.id].principal),
            "pending_budget": str(sum((o.amount for o in state.occurrences.values() if o.status == "PENDING_FUNDS"), Decimal(0))),
            "costs": str(sum((r.fill.fee + r.fill.tax for r in state.fills.values()), Decimal(0)))}
        checksum = sha256(canonical({"metrics": metrics, "valuations": [asdict(v) for v in valuations],
                                     "trades": trades_json, "status": status}).encode()).hexdigest()
        return RunResult(run_id, status, manifest_json, metrics, tuple(valuations), trades_json, checksum)


@dataclass(frozen=True)
class SimulationRun:
    id: str
    config: RunConfig
    bars: tuple[MarketBar, ...]
    status: str = "QUEUED"
    result: RunResult | None = None
    cancel_requested: bool = False
    progress: int = 0
    last_event_id: str | None = None
    error_code: str | None = None
    worker_token: str | None = None
    lease_until: datetime | None = None
