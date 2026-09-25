"""Route modules for the panel.

Each module exposes ``register(app)`` and owns one area of the panel, so adding
a screen means adding one file rather than growing a single router.
"""
from aiohttp import web

from app.web.routes import auth_routes, overview


def register(app: web.Application) -> None:
    auth_routes.register(app)
    overview.register(app)


__all__ = ["register"]
