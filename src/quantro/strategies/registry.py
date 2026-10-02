from jsonschema import Draft202012Validator, ValidationError
from quantro.domain.models import Conflict, DomainError
from quantro.domain.strategy import TradingStrategy


class StrategyRegistry:
    """Only reviewed installed plugins can be registered; no runtime code uploads."""

    def __init__(self):
        self._plugins: dict[tuple[str, str], TradingStrategy] = {}

    def register(self, plugin: TradingStrategy) -> None:
        key = (plugin.plugin_id, plugin.version)
        if key in self._plugins:
            raise Conflict("Plugin version already registered")
        Draft202012Validator.check_schema(plugin.config_schema)
        self._plugins[key] = plugin

    def get(self, plugin_id: str, version: str) -> TradingStrategy:
        try:
            return self._plugins[(plugin_id, version)]
        except KeyError as exc:
            raise DomainError("Unknown installed strategy version") from exc

    def validate_config(self, plugin_id: str, version: str, config: dict) -> None:
        plugin = self.get(plugin_id, version)
        try:
            Draft202012Validator(plugin.config_schema).validate(config)
        except ValidationError as exc:
            raise DomainError(f"Invalid strategy configuration: {exc.message}") from exc

    def descriptors(self) -> list[dict]:
        from copy import deepcopy
        return [{"plugin_id": p.plugin_id, "version": p.version,
                 "config_schema": deepcopy(p.config_schema), "warmup_bars": p.warmup_bars,
                 "required_fields": ["close"], "evaluation_trigger": "DAILY_BAR"}
                for p in self._plugins.values()]
