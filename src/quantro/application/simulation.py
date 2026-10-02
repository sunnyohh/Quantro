from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from typing import Callable
from uuid import uuid4

from quantro.domain.models import Conflict, DomainError
from quantro.ports.clock import Clock
from quantro.ports.unit_of_work import UnitOfWork
from quantro.simulation.engine import BacktestEngine, RunConfig, SimulationRun
from quantro.strategies.examples import default_registry


class SimulationService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock,
                 id_factory: Callable[[], str] = lambda: str(uuid4())):
        self.uow_factory, self.clock, self.id_factory = uow_factory, clock, id_factory

    def enqueue(self, config: RunConfig, bars: tuple) -> SimulationRun:
        default_registry().validate_config(config.plugin_id, config.plugin_version, dict(config.strategy_config))
        if not bars:
            raise DomainError("At least one daily market bar is required")
        if any(bar.instrument_id != config.instrument.id for bar in bars) or len({bar.id for bar in bars}) != len(bars):
            raise DomainError("Invalid simulation dataset")
        with self.uow_factory() as uow:
            run = SimulationRun(self.id_factory(), config, bars)
            uow.state.simulation_runs[run.id] = run
            uow.commit()
            return run

    def cancel(self, run_id: str):
        with self.uow_factory() as uow:
            run = uow.state.simulation_runs[run_id]
            if run.status in {"COMPLETED", "FAILED", "CANCELED"}:
                return run
            run = replace(run, cancel_requested=True, status="CANCELED" if run.status == "QUEUED" else run.status)
            uow.state.simulation_runs[run_id] = run
            uow.commit()
            return run

    def run_next(self) -> str | None:
        token = self.id_factory()
        with self.uow_factory() as uow:
            # Expired simulation leases can be replayed from immutable inputs.
            for run in tuple(uow.state.simulation_runs.values()):
                if run.status == "RUNNING" and run.lease_until is not None and run.lease_until <= self.clock.now():
                    uow.state.simulation_runs[run.id] = replace(run, status="QUEUED", worker_token=None, lease_until=None)
            candidates = [r for r in uow.state.simulation_runs.values() if r.status == "QUEUED"]
            if not candidates:
                uow.commit()
                return None
            run = candidates[0]
            run = replace(run, status="RUNNING", worker_token=token,
                          lease_until=self.clock.now() + timedelta(minutes=5))
            uow.state.simulation_runs[run.id] = run
            uow.commit()
        def canceled():
            with self.uow_factory() as uow:
                current = uow.state.simulation_runs[run.id]
                return current.cancel_requested or current.worker_token != token
        def progress(count, event_id):
            with self.uow_factory() as uow:
                current = uow.state.simulation_runs[run.id]
                if current.worker_token != token:
                    raise Conflict("Simulation worker lease was lost")
                uow.state.simulation_runs[run.id] = replace(current, progress=count, last_event_id=event_id,
                                                          lease_until=self.clock.now() + timedelta(minutes=5))
                uow.commit()
        try:
            result = BacktestEngine().run(run.config, run.bars, cancel_requested=canceled, on_progress=progress)
            with self.uow_factory() as uow:
                current = uow.state.simulation_runs[run.id]
                if current.worker_token == token:
                    uow.state.simulation_runs[run.id] = replace(current, status=result.status, result=result,
                                                              worker_token=None, lease_until=None)
                    uow.commit()
        except Exception as exc:
            with self.uow_factory() as uow:
                current = uow.state.simulation_runs[run.id]
                if current.worker_token == token:
                    uow.state.simulation_runs[run.id] = replace(current, status="FAILED", error_code=type(exc).__name__,
                                                              worker_token=None, lease_until=None)
                    uow.commit()
        return run.id
