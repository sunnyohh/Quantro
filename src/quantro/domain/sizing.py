from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .models import Allocation, DomainError, Instrument, money


@dataclass(frozen=True)
class BuySize:
    quantity: int
    target_amount: Decimal
    reserved_amount: Decimal
    reason: str


def validate_ratio(ratio: Decimal) -> None:
    if not isinstance(ratio, Decimal) or not ratio.is_finite() or not 0 < ratio <= 1:
        raise DomainError("Ratio must be a Decimal in (0, 1]")


def size_buy(allocation: Allocation, instrument: Instrument, ratio: Decimal,
             price: Decimal, buying_power: Decimal,
             fee_upper_bound: Callable[[int], Decimal]) -> BuySize:
    """Fee upper bounds must be nonnegative and monotonically increasing."""
    allocation.validate()
    validate_ratio(ratio)
    money(price, positive=True)
    money(buying_power)
    target = allocation.principal * ratio
    cap = min(target, allocation.cash_available, buying_power)
    lot = instrument.lot_size
    low, high = 0, int(cap // (price * lot))
    while low < high:
        middle = (low + high + 1) // 2
        quantity = middle * lot
        cost = quantity * price + money(fee_upper_bound(quantity))
        if cost <= cap:
            low = middle
        else:
            high = middle - 1
    quantity = low * lot
    reserved = quantity * price + money(fee_upper_bound(quantity)) if quantity else Decimal(0)
    reason = "SKIPPED" if not quantity else ("CLIP_TO_AVAILABLE" if cap < target else "SIZED")
    return BuySize(quantity, target, reserved, reason)


def size_sell(allocation: Allocation, instrument: Instrument, ratio: Decimal) -> int:
    allocation.validate()
    validate_ratio(ratio)
    return int(allocation.quantity_available * ratio // instrument.lot_size) * instrument.lot_size
