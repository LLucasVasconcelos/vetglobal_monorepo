from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- database -----------------------------------------------------------
    db_user: str = "vetglobal"
    db_password: str = "vetglobal"
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "vetglobal"

    # --- auth (D26 / D27) ---------------------------------------------------
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    internal_token: str = "dev-only-change-me"

    # --- long polling (D13 / D19) -------------------------------------------
    poll_timeout_seconds: int = 25
    poll_recheck_seconds: int = 5

    # --- upload validation (D25) --------------------------------------------
    max_upload_bytes: int = 10 * 1024 * 1024
    min_content_chars: int = 20

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. asyncpg is the driver for both app and migrations."""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
