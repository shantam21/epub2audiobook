"""Web UI: a FastAPI backend and the built React front end it serves."""

from .app import create_app

__all__ = ["create_app"]
