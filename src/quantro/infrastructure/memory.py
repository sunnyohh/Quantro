from copy import deepcopy
from threading import RLock
from quantro.domain.models import State


class MemoryStore:
    """Process-local development adapter; never use for durable/live trading."""

    def __init__(self) -> None:
        self._state = State()
        self._lock = RLock()

    def snapshot(self) -> State:
        with self._lock:
            return deepcopy(self._state)

    def unit_of_work(self) -> "MemoryUnitOfWork":
        return MemoryUnitOfWork(self)


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def __enter__(self) -> "MemoryUnitOfWork":
        self.store._lock.acquire()
        self.state = deepcopy(self.store._state)
        return self

    def commit(self) -> None:
        self.state.validate()
        self.store._state = deepcopy(self.state)

    def rollback(self) -> None:
        self.state = deepcopy(self.store._state)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.store._lock.release()
