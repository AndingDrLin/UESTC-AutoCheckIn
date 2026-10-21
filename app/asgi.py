"""ASGI entry point used by ``uvicorn``.

Keeping the factory in :mod:`app.main` and the instantiation here means that
importing ``app.main`` (tests, CLI helpers) has no filesystem side effects.
"""
from .main import create_app

app = create_app()
