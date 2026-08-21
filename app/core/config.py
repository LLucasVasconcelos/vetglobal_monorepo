from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment (or a local .env file).

    Every field here has a matching entry in `.env.example`, and
    `tests/test_config.py` fails if the two ever drift apart.

    Secrets have no default on purpose: the process refuses to start without
    them, the same way `docker-compose.yml` refuses to start Postgres without
    `DB_PASSWORD`. A signed token is only worth the secrecy of its key -- with a
    fallback value, anyone who read this repository could mint a token for any
    tenant and walk straight past the isolation of D26.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- database -----------------------------------------------------------
    db_user: str = "vetglobal"
    db_password: str
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "vetglobal"

    # --- auth (D26 / D27) ---------------------------------------------------
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    internal_token: str

    # --- long polling (D13 / D19) -------------------------------------------
    # Ceiling of the HTTP request. Hitting it is not a job failure: the answer
    # is `timed_out: true` and the client asks again (invariant 6).
    poll_timeout_seconds: int = 25
    # Safety net. LISTEN/NOTIFY is not persistent, so a notification that
    # arrives with nobody listening is recovered by this re-query -- it is what
    # makes the design correct, not merely fast.
    poll_recheck_seconds: int = 5

    # --- job lifecycle (D21) ------------------------------------------------
    # Ceiling of the *processing*, the other clock entirely. A job still
    # PROCESSING past `claimed_at + job_lease_seconds` was abandoned by its
    # worker: it goes back to the queue, or fails with PROCESSING_TIMEOUT once
    # `job_max_attempts` is spent.
    job_lease_seconds: int = 60
    job_max_attempts: int = 3

    # --- upload validation (D25) --------------------------------------------
    max_upload_bytes: int = 10 * 1024 * 1024
    min_content_chars: int = 20

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. asyncpg is the driver for both app and migrations.

        User and password are percent-encoded: a password holding `@`, `/` or
        `#` would otherwise be parsed as part of the host.
        """
        return (
            f"postgresql+asyncpg://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
