from decimal import Decimal

from quantro.domain.strategy import StrategyContext
from quantro.domain.trading import OrderIntent, Side


class BudgetBuyStrategy:
    """Contract example: buy each confirmed bar if repeat is explicitly enabled."""
    plugin_id = "budget-buy"
    version = "1.0.0"
    warmup_bars = 1
    config_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["ratio", "repeat"],
        "properties": {"ratio": {"type": "string", "pattern": r"^(0\.[0-9]*[1-9][0-9]*|1(\.0+)?)$"},
                       "repeat": {"type": "boolean"}},
    }

    def evaluate(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        config = dict(context.config)
        if not context.bars or (not config["repeat"] and context.quantity_available > 0):
            return ()
        return (OrderIntent(context.evaluation_id + ":buy", context.allocation_id,
                            context.assignment_id, Side.BUY, "BUDGET_PERCENT", Decimal(config["ratio"]),
                            context.evaluation_id, "Confirmed daily budget purchase"),)


class MovingAverageStrategy:
    plugin_id = "moving-average"
    version = "1.0.0"
    warmup_bars = 2
    config_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["period", "buy_ratio", "sell_ratio", "repeat"],
        "properties": {
            "period": {"type": "integer", "minimum": 2, "maximum": 250},
            "buy_ratio": BudgetBuyStrategy.config_schema["properties"]["ratio"],
            "sell_ratio": BudgetBuyStrategy.config_schema["properties"]["ratio"],
            "repeat": {"type": "boolean"},
        },
    }

    def evaluate(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        config = dict(context.config)
        period = config["period"]
        if len(context.bars) < period:
            return ()
        average = sum((b.close for b in context.bars[-period:]), Decimal(0)) / period
        close = context.bars[-1].close
        if close > average and (config["repeat"] or context.quantity_available == 0):
            side, method, ratio = Side.BUY, "BUDGET_PERCENT", Decimal(config["buy_ratio"])
        elif close < average and context.quantity_available > 0:
            side, method, ratio = Side.SELL, "POSITION_PERCENT", Decimal(config["sell_ratio"])
        else:
            return ()
        return (OrderIntent(context.evaluation_id + ":signal", context.allocation_id,
                            context.assignment_id, side, method, ratio,
                            context.evaluation_id, "Close versus moving average"),)


def default_registry():
    from .registry import StrategyRegistry
    registry = StrategyRegistry()
    registry.register(BudgetBuyStrategy())
    registry.register(MovingAverageStrategy())
    return registry
