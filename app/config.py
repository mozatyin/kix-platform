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
