"""Application configuration loaded from environment variables.

Cloud Run injects these at runtime:
- Plain values via `--set-env-vars` in cloudbuild.yaml
- Secrets via `--set-secrets` (mounted as env vars from Secret Manager)

For local dev, copy `.env.example` to `.env` and fill in values.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === GCP project ===
    gcp_project_id: str = Field(default="sailline", alias="GCP_PROJECT_ID")

    # === Cloud SQL ===
    # Format: "project:region:instance" — e.g. "sailline:us-central1:sailline-db"
    cloud_sql_instance: str = Field(alias="CLOUD_SQL_INSTANCE")
    db_user: str = Field(alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")  # injected from Secret Manager
    db_name: str = Field(alias="DB_NAME")

    # === Memorystore Redis (used in week 2) ===
    redis_host: str | None = Field(default=None, alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")

    # === Cloud Storage ===
    gcs_weather_bucket: str | None = Field(default=None, alias="GCS_WEATHER_BUCKET")


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — Settings() is resolved once per process."""
    return Settings()


# Convenience module-level handle.
settings = get_settings()