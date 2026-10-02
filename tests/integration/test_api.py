from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from quantro.api.app import create_app
from quantro.application.simulation import SimulationService
from quantro.infrastructure.database.store import DatabaseStore


class Clock:
    time = datetime.fromisoformat("2026-01-01T09:00:00+09:00")
    def now(self):
        return self.time


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = DatabaseStore("sqlite:///" + (Path(self.temp.name) / "api.db").as_posix())
        self.store.initialize_local()
        self.clock = Clock()
        self.headers = {"Authorization": "Bearer quantro-test-token-long-enough"}
        self.client = TestClient(create_app(self.store, token="quantro-test-token-long-enough", clock=self.clock))
        self.account = self.post("/accounts", {"initial_cash": "10000000"}, "account").json()
        self.instrument = self.post("/instruments", {"symbol": "TEST"}, "instrument").json()
        self.allocation = self.post(f"/accounts/{self.account['id']}/allocations",
            {"instrument_id": self.instrument["id"], "name": "Test"}, "allocation").json()

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.temp.cleanup()

    def post(self, path, body=None, key="key"):
        return self.client.post("/api/v1" + path, json=body,
                                headers={**self.headers, "Idempotency-Key": key})

    def get(self, path):
        return self.client.get("/api/v1" + path, headers=self.headers)

    def test_authentication_and_mutation_key_required(self):
        self.assertEqual(self.client.get("/api/v1/accounts").status_code, 401)
        response = self.client.post("/api/v1/accounts", json={"initial_cash": "1"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertIn("correlation_id", response.json())

    def test_decimal_strings_idempotency_conflict_and_stale_revision(self):
        path = f"/allocations/{self.allocation['id']}/budget/additions"
        body = {"amount": "1000000", "expected_revision": 0}
        first = self.post(path, body, "same")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(self.post(path, body, "same").json(), first.json())
        self.assertEqual(first.json()["principal"], "1000000")
        self.assertEqual(self.post(path, {**body, "amount": "2000000"}, "same").status_code, 409)
        self.assertEqual(self.post(path, body, "new").status_code, 409)
        self.assertEqual(self.post(path, {**body, "amount": 1000000}, "float").status_code, 422)
        self.assertEqual(self.post(path, {**body, "amount": "NaN"}, "nan").status_code, 422)

    def test_mutation_and_cached_response_are_atomic_after_restart(self):
        path = f"/accounts/{self.account['id']}/allocations"
        # A distinct instrument is needed because allocation uniqueness is enforced.
        instrument = self.post("/instruments", {"symbol": "OTHER"}, "other").json()
        body = {"instrument_id": instrument["id"], "name": "Other"}
        first = self.post(path, body, "once")
        self.assertEqual(first.status_code, 201)
        new_store = DatabaseStore(str(self.store.engine.url))
        try:
            with TestClient(create_app(new_store, token="quantro-test-token-long-enough", clock=self.clock)) as client:
                response = client.post("/api/v1" + path, json=body,
                    headers={**self.headers, "Idempotency-Key": "once"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json(), first.json())
            self.assertEqual(len(new_store.snapshot().allocations), 2)
        finally:
            new_store.close()

    def test_dashboard_budget_distribution(self):
        self.post(f"/allocations/{self.allocation['id']}/budget/additions", {"amount": "1000000", "expected_revision": 0})
        dashboard = self.get(f"/accounts/{self.account['id']}/dashboard").json()
        self.assertEqual(dashboard["account_nav"], "10000000")
        self.assertEqual(dashboard["unallocated_cash"], "9000000")
        self.assertEqual(dashboard["budget_distribution"][0]["amount"], "1000000")

    def test_schedule_pause_preserves_history_and_stops_future_occurrences(self):
        path = f"/allocations/{self.allocation['id']}"
        response = self.post(path + "/schedule", {"amount": "300000", "frequency": "WEEKLY", "expected_revision": 0})
        self.assertEqual(response.status_code, 201, response.text)
        self.clock.time = datetime.fromisoformat("2026-01-08T09:00:00+09:00")
        response = self.post(path + "/schedule/pause", {"expected_revision": 1}, "pause")
        self.assertEqual(response.status_code, 200, response.text)
        self.clock.time = datetime.fromisoformat("2026-01-22T09:00:00+09:00")
        from quantro.application.budget import BudgetService
        BudgetService(self.store.unit_of_work, self.clock).process_due(self.clock.now())
        detail = self.get(path).json()
        self.assertEqual(detail["principal"], "300000")
        self.assertEqual(len(self.get(path + "/budget/history").json()["occurrences"]), 1)

    def run_body(self):
        bars = [{"id": f"bar:{day}", "start_at": f"2026-01-{day:02d}T09:00:00+09:00",
                 "end_at": f"2026-01-{day:02d}T15:30:00+09:00", "available_at": f"2026-01-{day:02d}T15:30:00+09:00",
                 "open": "100", "high": "110", "low": "90", "close": "100", "volume": 10000} for day in (5, 6, 7)]
        return {"instrument_id": self.instrument["id"], "start_at": "2026-01-05T08:00:00+09:00",
            "end_at": "2026-01-08T08:00:00+09:00", "initial_cash": "1000000", "initial_budget": "100000",
            "plugin_id": "budget-buy", "plugin_version": "1.0.0", "config": {"ratio": "1", "repeat": False},
            "risk": {"version": "fixture-v1", "max_order_amount": "1000000", "max_daily_amount": "1000000",
                     "max_quote_age_seconds": 60, "fee_rate": "0"},
            "dataset_id": "fixture-v1", "code_version": "fixture-code-v1", "participation": "1",
            "slippage_rate": "0", "sell_tax_rate": "0", "bars": bars}

    def test_simulation_worker_persistence_and_comparison(self):
        first = self.post("/simulation-runs", self.run_body(), "first")
        second = self.post("/simulation-runs", self.run_body(), "second")
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        worker = SimulationService(self.store.unit_of_work, self.clock)
        worker.run_next()
        worker.run_next()
        one = self.get(f"/simulation-runs/{first.json()['id']}").json()
        two = self.get(f"/simulation-runs/{second.json()['id']}").json()
        self.assertEqual(one["status"], "COMPLETED", one)
        self.assertEqual(one["result"]["checksum"], two["result"]["checksum"])
        self.assertEqual(one["result"]["metrics"]["fill_count"], 1)
        compared = self.get(f"/simulation-comparisons?run_ids={one['id']},{two['id']}")
        self.assertEqual(compared.status_code, 200, compared.text)
        self.assertTrue(compared.json()["same_comparison_group"])

    def test_queued_simulation_cancellation(self):
        run = self.post("/simulation-runs", self.run_body(), "cancel-run").json()
        self.post(f"/simulation-runs/{run['id']}/cancel", key="cancel")
        self.assertIsNone(SimulationService(self.store.unit_of_work, self.clock).run_next())
        self.assertEqual(self.get(f"/simulation-runs/{run['id']}").json()["status"], "CANCELED")
