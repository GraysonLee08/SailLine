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
    # PHRF cert PDFs uploaded via /api/boats/{id}/cert. Nullable so dev
    # works without provisioning the bucket — the boats router falls
    # back to "parse but don't persist" when unset.
    gcs_certs_bucket: str | None = Field(default=None, alias="GCS_CERTS_BUCKET")

    # === Anthropic (post-race AI summary, Session D1) ===
    # Nullable so the app boots without the key — the summary service
    # returns None on missing/blank, the stats view degrades gracefully
    # to "summary unavailable" rather than 500ing.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="ANTHROPIC_MODEL"
    )

    # === Cloud Run Job: race-postprocess (Session D1) ===
    # Fully-qualified job name used by ``app/services/job_trigger.py``
    # to fire-and-forget the post-race postprocess. Format:
    #   projects/{project}/locations/{region}/jobs/{job}
    # Nullable so dev/local runs no-op the trigger silently (jobs only
    # exist in deployed environments).
    race_postprocess_job: str | None = Field(
        default=None, alias="RACE_POSTPROCESS_JOB"
    )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — Settings() is resolved once per process."""
    return Settings()


# Convenience module-level handle.
settings = get_settings()
