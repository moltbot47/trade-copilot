"""Application settings sourced from .env via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    DATABASE_URL: str = "sqlite:///./trade_copilot.db"
    SECRET_KEY: str = "change_me_to_random_64_chars"
    TRADELOCKER_API_BASE: str = "https://live.tradelocker.com/backend-api"
    TRADELOCKER_DEMO_API_BASE: str = "https://demo.tradelocker.com/backend-api"
    DISCORD_WEBHOOK_URL: str = ""
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001"
    ENCRYPTION_KEY: str = "change_me_fernet_key_32_bytes_b64"
    BUY_ME_COFFEE_USERNAME: str = "dbutler"
    ENVIRONMENT: str = "development"  # production | staging | development

    # Session cookie config — secure by default in production, off for localhost.
    # Override via env var (SESSION_COOKIE_SECURE=true|false) if needed.
    SESSION_COOKIE_SECURE: bool | None = None
    SESSION_COOKIE_NAME: str = "tc_session"
    SESSION_TTL_DAYS: int = 30

    @property
    def cookie_secure(self) -> bool:
        if self.SESSION_COOKIE_SECURE is not None:
            return self.SESSION_COOKIE_SECURE
        return self.ENVIRONMENT.lower() == "production"

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    def assert_production_safe(self) -> None:
        """Refuse to boot in production with placeholder secrets."""
        if self.ENVIRONMENT.lower() != "production":
            return
        bad = []
        if self.SECRET_KEY.startswith("change_me"):
            bad.append("SECRET_KEY")
        if self.ENCRYPTION_KEY.startswith("change_me"):
            bad.append("ENCRYPTION_KEY")
        if bad:
            raise RuntimeError(
                f"refusing to start in production with placeholder secrets: {bad}"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
