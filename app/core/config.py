from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel only: production refuses to start with it.
DEV_SECRET = "dev-insecure-change-me"  # noqa: S105  # nosec B105


class Settings(BaseSettings):
    # hide_input_in_errors: a failed startup check must not print DATABASE_URL (has password)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://pfc:change-me-db@localhost:5432/pfc"
    redis_url: str = "redis://:change-me-redis@localhost:6379/0"
    secret_key: SecretStr = SecretStr(DEV_SECRET)
    allowed_origins: list[str] = ["http://localhost:3000"]
    # Ed25519 private key (PEM) for access JWTs. Dev/test generate a throwaway
    # key per process when unset; production requires it.
    jwt_private_key: SecretStr | None = None
    access_ttl_seconds: int = 600
    refresh_ttl_days_remember: int = 30
    refresh_ttl_hours_session: int = 12
    # Online payments. "fake" = in-process stand-in for dev/tests (refused in prod).
    payment_provider: Literal["fake", "razorpay"] = "fake"
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    @property
    def is_prod(self) -> bool:
        return self.env == "production"

    @property
    def cookie_secure(self) -> bool:
        # Secure cookies need https; dev runs over http (incl. phone on LAN).
        return self.is_prod

    @model_validator(mode="after")
    def _prod_guard(self) -> Self:
        if self.is_prod:
            secret = self.secret_key.get_secret_value()
            if secret == DEV_SECRET or len(secret) < 32:
                raise ValueError(
                    "SECRET_KEY must be set to a random value of 32+ chars in production"
                )
            if self.jwt_private_key is None:
                raise ValueError("JWT_PRIVATE_KEY is required in production")
            if any(o.startswith("http://") and "localhost" not in o for o in self.allowed_origins):
                raise ValueError("ALLOWED_ORIGINS must be https in production")
            if self.payment_provider != "razorpay" or not (
                self.razorpay_key_id and self.razorpay_key_secret and self.razorpay_webhook_secret
            ):
                raise ValueError("Razorpay keys and webhook secret are required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
