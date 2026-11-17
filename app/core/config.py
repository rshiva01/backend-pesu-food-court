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

    @property
    def is_prod(self) -> bool:
        return self.env == "production"

    @model_validator(mode="after")
    def _prod_guard(self) -> Self:
        if self.is_prod:
            secret = self.secret_key.get_secret_value()
            if secret == DEV_SECRET or len(secret) < 32:
                raise ValueError(
                    "SECRET_KEY must be set to a random value of 32+ chars in production"
                )
            if any(o.startswith("http://") and "localhost" not in o for o in self.allowed_origins):
                raise ValueError("ALLOWED_ORIGINS must be https in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
