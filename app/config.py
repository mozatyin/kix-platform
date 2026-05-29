"""KiX Platform configuration via Pydantic Settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    # ── PostgreSQL ────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "kix"
    postgres_user: str = "mozat"
    postgres_password: str = ""

    # Optional explicit override (e.g. for managed Postgres / pgbouncer).
    # If set, takes precedence over the host/port/user/db fields below.
    database_url_override: str | None = None

    # Optional read-replica DSN — when set, read-only endpoints can route
    # through ``get_read_db()`` and target this engine instead of primary.
    # Falls back to the primary DSN if unset.
    database_read_url: str | None = None

    # ── PostgreSQL connection pool ────────────────────────────────────────
    # Defaults bumped from (20, 10) → (50, 100) = 150 max connections.
    db_pool_size: int = 50
    db_max_overflow: int = 100
    db_pool_timeout: int = 30  # seconds to wait for a free connection
    db_pool_recycle: int = 3600  # recycle connections after 1h
    db_pool_pre_ping: bool = True  # validate connection before checkout

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT ───────────────────────────────────────────────────────────────
    jwt_secret: str = "kix-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 15  # R5: short-lived 15-minute JWT

    # ── QR ────────────────────────────────────────────────────────────────
    qr_signing_secret: str = "kix-qr-secret-change-in-production"

    # ── General ───────────────────────────────────────────────────────────
    env: str = "development"
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        """Build the async database URL for asyncpg."""
        if self.database_url_override:
            return self.database_url_override
        password_part = f":{self.postgres_password}" if self.postgres_password else ""
        return (
            f"postgresql+asyncpg://{self.postgres_user}{password_part}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
