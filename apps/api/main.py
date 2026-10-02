"""Use: python -m uvicorn quantro.api.app:app_factory --factory --host 127.0.0.1."""
from quantro.api.app import app_factory

__all__ = ["app_factory"]
