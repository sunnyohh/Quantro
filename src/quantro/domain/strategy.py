from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .trading import MarketBar, OrderIntent


@dataclass(frozen=True)
class StrategyAssignment:
    id: str
    allocation_id: str
    plugin_id: str
    plugin_version: str
    config: tuple[tuple[str, str | int | bool], ...]
    priority: int
    revision: int = 0
    enabled: bool = True
    archived: bool = False


@dataclass(frozen=True)
class StrategyContext:
    evaluation_id: str
    assignment_id: str
    allocation_id: str
    event_time: datetime
    bars: tuple[MarketBar, ...]
    principal: Decimal
    cash_available: Decimal
    quantity_available: int
    config: tuple[tuple[str, str | int | bool], ...]


class TradingStrategy(Protocol):
    plugin_id: str
    version: str
    config_schema: dict
    warmup_bars: int

    def evaluate(self, context: StrategyContext) -> tuple[OrderIntent, ...]: ...
