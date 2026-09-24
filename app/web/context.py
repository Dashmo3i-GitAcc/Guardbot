"""Typed keys for per-request and per-application state.

aiohttp wants ``AppKey``/``RequestKey`` objects rather than bare strings, so a
typo cannot silently create a second, empty slot — and the deprecation warnings
stay out of the test output.
"""
from aiohttp import web

# Per-request
SESSION = web.RequestKey("session")
AUDIENCE = web.RequestKey("audience")
PRINCIPAL = web.RequestKey("principal")

# Per-application
STARTED_AT = web.AppKey("started_at", float)

__all__ = ["AUDIENCE", "PRINCIPAL", "SESSION", "STARTED_AT"]
