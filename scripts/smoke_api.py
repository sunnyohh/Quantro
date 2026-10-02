"""Start the real API/worker against a temporary local database and verify HTTP."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="quantro-smoke-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "QUANTRO_DATABASE_URL": "sqlite:///" + (Path(directory) / "smoke.db").as_posix(),
               "QUANTRO_API_TOKEN": secrets.token_urlsafe(32)}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with open(Path(directory) / "server.log", "w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "quantro.api.app:app_factory",
                "--factory", "--host", "127.0.0.1", "--port", str(port)], cwd=root, env=env,
                stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            try:
                headers = {"Authorization": "Bearer " + env["QUANTRO_API_TOKEN"]}
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            response = client.get("/api/v1/health", headers=headers)
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise RuntimeError("API did not become ready")
                        time.sleep(.2)
                    assert client.get("/api/v1/accounts").status_code == 401
                    headers["Idempotency-Key"] = "smoke-account"
                    body = {"initial_cash": "1000000", "execution_mode": "PAPER"}
                    first = client.post("/api/v1/accounts", json=body, headers=headers)
                    second = client.post("/api/v1/accounts", json=body, headers=headers)
                    assert first.status_code == second.status_code == 201
                    assert first.json() == second.json()
                    assert len(client.get("/api/v1/accounts", headers=headers).json()) == 1
                subprocess.run([sys.executable, "apps/worker/main.py", "--once"], cwd=root, env=env,
                               check=True, timeout=15, creationflags=flags)
                print("API HTTP/auth/idempotency and worker process smoke checks passed")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
