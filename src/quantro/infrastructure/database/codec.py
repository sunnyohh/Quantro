"""Explicit JSON value codec; no executable pickles or dynamic imports."""
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from quantro.domain import models, schedule, strategy, trading
from quantro.simulation import engine


CLASSES = {cls.__name__: cls for module in (models, schedule, strategy, trading, engine)
           for cls in vars(module).values() if isinstance(cls, type) and is_dataclass(cls)}
ENUMS = {cls.__name__: cls for cls in (schedule.Frequency, trading.Side, trading.OrderState)}


def encode(value):
    if isinstance(value, Enum):
        return {"tag": "enum", "type": type(value).__name__, "value": value.value}
    if isinstance(value, Decimal):
        return {"tag": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        models.aware(value)
        return {"tag": "datetime", "value": value.astimezone(timezone.utc).isoformat()}
    if is_dataclass(value):
        return {"tag": "object", "type": type(value).__name__,
                "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {"tag": "tuple", "items": [encode(v) for v in value]}
    if isinstance(value, list):
        return {"tag": "list", "items": [encode(v) for v in value]}
    if isinstance(value, dict):
        return {"tag": "dict", "items": [[encode(k), encode(v)] for k, v in value.items()]}
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"Unsupported persisted type: {type(value).__name__}")


def decode(value):
    if not isinstance(value, dict):
        return value
    tag = value["tag"]
    if tag == "decimal":
        return Decimal(value["value"])
    if tag == "datetime":
        return datetime.fromisoformat(value["value"])
    if tag == "enum":
        return ENUMS[value["type"]](value["value"])
    if tag == "object":
        return CLASSES[value["type"]](**{k: decode(v) for k, v in value["fields"].items()})
    if tag == "tuple":
        return tuple(decode(v) for v in value["items"])
    if tag == "list":
        return [decode(v) for v in value["items"]]
    if tag == "dict":
        return {decode(k): decode(v) for k, v in value["items"]}
    raise ValueError("Unknown persisted value tag")
