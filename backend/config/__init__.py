"""Gobanion Configuration System

Multi-environment config via .env files.
Environment chain: .env.shared -> .env.{ENV} -> .env.local (overrides in order)
"""

from .settings import get_settings, GobanionSettings

__all__ = ["get_settings", "GobanionSettings"]
