"""
Gobanion settings via pydantic-settings.

Load chain:
  1. .env.shared       – values shared across all environments (committed to git)
  2. .env.{ENV}        – environment-specific defaults (committed)
  3. .env.local        – local overrides (gitignored, never committed)
  4. OS environment variables (highest priority)

Usage:
    from config import get_settings
    settings = get_settings()
    print(settings.LLM_API_BASE)
"""

import os
from pathlib import Path
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator

# ── Project root (where .env files live) ────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # backend/

# ── Environment detection ───────────────────────────────────────────
APP_ENV = os.getenv("APP_ENV", "development")


def _load_env_chain() -> None:
    """Load .env files in priority order (lowest → highest).

    OS environment variables always take highest priority, so .env files
    use override=False (don't clobber existing env vars). Only .env.local
    uses override=True since it's explicitly meant as a local override.
    """
    env_files = [
        (PROJECT_ROOT / ".env.shared", False),
        (PROJECT_ROOT / f".env.{APP_ENV}", False),
        (PROJECT_ROOT / ".env.local", True),
    ]
    for f, override in env_files:
        if f.exists():
            load_dotenv(f, override=override)


_load_env_chain()


# ── Settings class ──────────────────────────────────────────────────


class DatabaseSettings(BaseSettings):
    """Database connection settings."""
    URL: str = Field(default="sqlite:///./gobanion.db", alias="DATABASE_URL")
    ECHO: bool = Field(default=False, alias="DATABASE_ECHO")


class LLMSettings(BaseSettings):
    """LLM inference backend settings."""
    API_BASE: str = Field(default="http://localhost:8000/v1", alias="LLM_API_BASE")
    API_KEY: str = Field(default="", alias="LLM_API_KEY")
    MODEL: str = Field(default="Qwen3.5-9B", alias="LLM_MODEL")
    MAX_TOKENS: int = Field(default=8192, alias="LLM_MAX_TOKENS")
    TEMPERATURE: float = Field(default=0.7, alias="LLM_TEMPERATURE")
    TIMEOUT: int = Field(default=120, alias="LLM_TIMEOUT")

    # Fallback / public model (主 Agent 规划用)
    FALLBACK_BASE: str = Field(default="https://api.deepseek.com", alias="LLM_FALLBACK_BASE")
    FALLBACK_KEY: str = Field(default="", alias="LLM_FALLBACK_KEY")
    FALLBACK_MODEL: str = Field(default="deepseek-v4-flash", alias="LLM_FALLBACK_MODEL")
    FALLBACK_MAX_TOKENS: int = Field(default=16384, alias="LLM_FALLBACK_MAX_TOKENS")

    @field_validator("TEMPERATURE")
    @classmethod
    def clamp_temperature(cls, v: float) -> float:
        return max(0.0, min(2.0, v))


class AgentSettings(BaseSettings):
    """Agent runner settings."""
    DISPATCHER_TIMEOUT: int = Field(default=30, alias="AGENT_DISPATCHER_TIMEOUT")
    HEARTBEAT_INTERVAL: int = Field(default=10, alias="AGENT_HEARTBEAT_INTERVAL")
    MAX_RETRIES: int = Field(default=3, alias="AGENT_MAX_RETRIES")
    EXECUTE_TIMEOUT: int = Field(default=120, alias="AGENT_EXECUTE_TIMEOUT")


class GobanionSettings(BaseSettings):
    """Root settings container."""

    # ── App metadata ──
    APP_NAME: str = "Gobanion"
    APP_VERSION: str = "0.1.0"
    APP_ENV: str = Field(default="development", alias="APP_ENV")

    # ── HTTP server ──
    HOST: str = Field(default="0.0.0.0", alias="HOST")
    PORT: int = Field(default=5000, alias="PORT")
    DEBUG: bool = Field(default=True, alias="DEBUG")
    CORS_ORIGINS: list[str] = Field(default=["*"], alias="CORS_ORIGINS")

    # ── Sub-settings (nested via model_config) ──
    database: DatabaseSettings = DatabaseSettings()
    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()

    model_config = {"env_nested_delimiter": "__", "case_sensitive": True}


# ── Singleton accessor ──────────────────────────────────────────────


@lru_cache()
def get_settings() -> GobanionSettings:
    """Return cached settings singleton."""
    return GobanionSettings()


__all__ = ["get_settings", "GobanionSettings"]
