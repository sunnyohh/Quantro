from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from .models import DomainError, aware, money


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


@dataclass
class BudgetScheduleVersion:
    id: str
    allocation_id: str
    version: int
    amount: Decimal
    frequency: Frequency
    anchor_local: datetime
    effective_from: datetime
    timezone_name: str = "Asia/Seoul"
    effective_to: datetime | None = None
    enabled: bool = True
    currency: str = "KRW"

    def __post_init__(self) -> None:
        money(self.amount, positive=True)
        aware(self.anchor_local)
        aware(self.effective_from)
        self.frequency = Frequency(self.frequency)
        if self.timezone_name != "Asia/Seoul" or self.currency != "KRW":
            raise DomainError("Initial schedules support Asia/Seoul and KRW")
        self.anchor_local = self.anchor_local.astimezone(ZoneInfo(self.timezone_name))

    def due_at(self, index: int) -> datetime:
        if index < 1:
            raise DomainError("First occurrence is one period after the anchor")
        anchor = self.anchor_local
        if self.frequency == Frequency.MONTHLY:
            month_index = anchor.year * 12 + anchor.month - 1 + index
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            result = anchor.replace(year=year, month=month,
                                    day=min(anchor.day, monthrange(year, month)[1]))
        else:
            days = index * (7 if self.frequency == Frequency.WEEKLY else 1)
            result = anchor + timedelta(days=days)
        return result.astimezone(timezone.utc)


@dataclass
class ScheduleOccurrence:
    schedule_version_id: str
    due_at: datetime
    amount: Decimal
    status: str = "PENDING_FUNDS"
    transaction_id: str | None = None
