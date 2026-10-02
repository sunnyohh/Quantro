from datetime import datetime, timezone


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)
