"""Fire-and-forget trigger for Cloud Run Jobs.

Currently used to kick off ``race-postprocess`` the moment the final
mark of a race is detected (see ``app/routers/tracks.py``).

This module is intentionally tiny and tolerant of every failure mode:
the trigger should never break user-facing endpoints. If the env var
is unset, the SDK can't get credentials, the API call fails — all of
that logs a warning and returns. The Cloud Run Job can always be
re-invoked manually:

    python -m workers.race_postprocess --race-id <uuid>

The Cloud Run v2 Admin API accepts a ``containerOverrides.args`` field
on ``:run`` calls, which is how we pass the race id through to the
job container. Auth: Application Default Credentials in production
(the API service account has ``roles/run.developer`` on the job).
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _get_access_token() -> Optional[str]:
    """Pull a bearer token from Application Default Credentials.

    Lazy-imports google.auth so this module is importable in test
    environments that don't have the SDK installed. Returns None on
    any failure — the caller no-ops cleanly.
    """
    try:
        import google.auth  # type: ignore[import-not-found]
        from google.auth.transport.requests import Request  # type: ignore[import-not-found]
    except ImportError as e:
        log.warning("job_trigger: google-auth not installed (%s)", e)
        return None
    try:
        creds, _ = google.auth.default(scopes=[_SCOPE])
        creds.refresh(Request())
        return creds.token
    except Exception as e:  # noqa: BLE001
        log.warning("job_trigger: failed to obtain ADC token (%s)", e)
        return None


async def trigger_race_postprocess(
    race_id: UUID, *, force: bool = False,
) -> None:
    """Kick off the race-postprocess Cloud Run Job for this race.

    Never raises. Logs a warning and returns when:
      * RACE_POSTPROCESS_JOB env var is unset (dev/test mode)
      * ADC unavailable
      * Network/HTTP error contacting Cloud Run

    The job is asynchronous from this caller's perspective — once
    Cloud Run accepts the request, the HTTP response returns
    immediately and the actual work happens out-of-band.
    """
    settings = get_settings()
    job_name = settings.race_postprocess_job
    if not job_name:
        log.info(
            "job_trigger: RACE_POSTPROCESS_JOB unset; skipping postprocess "
            "for race %s (this is fine in dev)", race_id,
        )
        return

    token = _get_access_token()
    if token is None:
        return

    args = ["--race-id", str(race_id)]
    if force:
        args.append("--force")
    body = {"overrides": {"containerOverrides": [{"args": args}]}}
    url = f"https://run.googleapis.com/v2/{job_name}:run"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code >= 300:
            log.warning(
                "job_trigger: %s returned %s: %s",
                url, resp.status_code, resp.text[:500],
            )
            return
        log.info(
            "job_trigger: kicked off race-postprocess for race %s "
            "(force=%s)", race_id, force,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "job_trigger: HTTP call to %s failed (%s)", url, e,
        )
