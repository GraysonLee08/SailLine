"""Tests for app/services/job_trigger.py.

The trigger is fire-and-forget and explicitly "never raises", so the
tests verify it logs + no-ops on every failure path AND that the happy
path posts the expected URL + body shape.

httpx is mocked by monkeypatching ``job_trigger.httpx.AsyncClient`` to
return a fake context manager. ADC is mocked by monkeypatching the
module-private ``_get_access_token`` to return a known string (or None).
get_settings is overridden via app.config.get_settings monkeypatch,
matching the pattern used in test_email_service.py.

pytest.ini sets ``asyncio_mode = auto`` so async tests don't need a
decorator — just ``async def test_…``.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from app.services import job_trigger as jt_mod
from app.services.job_trigger import trigger_race_postprocess


RACE_ID = UUID("11111111-2222-3333-4444-555555555555")


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient. Records the last POST call.

    Constructed with the kwargs httpx.AsyncClient receives (we ignore
    them), then used as an ``async with`` context manager whose .post
    returns a configured _FakeResponse — or raises if raise_exc is set.
    """

    # Class-level so tests can inspect the most recent call without
    # threading an instance reference through monkeypatching.
    last_url: str | None = None
    last_json: dict | None = None
    last_headers: dict | None = None
    next_response: _FakeResponse = _FakeResponse(200, "")
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs):
        # Reset per-construction so each test gets fresh capture state.
        type(self).last_url = None
        type(self).last_json = None
        type(self).last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None, **_):
        cls = type(self)
        cls.last_url = url
        cls.last_json = json
        cls.last_headers = headers
        if cls.raise_exc is not None:
            raise cls.raise_exc
        return cls.next_response


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch):
    """Replace httpx.AsyncClient inside the job_trigger module."""
    # Reset class-level state.
    _FakeAsyncClient.next_response = _FakeResponse(200, "")
    _FakeAsyncClient.raise_exc = None
    monkeypatch.setattr(jt_mod.httpx, "AsyncClient", _FakeAsyncClient)


def _set_settings(monkeypatch: pytest.MonkeyPatch, *, job: str | None):
    """Stub app.config.get_settings to return a settings-like object."""
    fake = type(
        "FakeSettings", (), {"race_postprocess_job": job},
    )()
    import app.config

    monkeypatch.setattr(app.config, "get_settings", lambda: fake)
    # job_trigger imports get_settings at the top; the monkeypatched
    # symbol in app.config isn't what's bound in the trigger's
    # namespace, so patch there too.
    monkeypatch.setattr(jt_mod, "get_settings", lambda: fake)


def _set_token(monkeypatch: pytest.MonkeyPatch, token: str | None):
    monkeypatch.setattr(jt_mod, "_get_access_token", lambda: token)


# ── trigger_race_postprocess: env / token short-circuits ──────────────


async def test_noop_when_job_env_unset(monkeypatch: pytest.MonkeyPatch):
    _set_settings(monkeypatch, job=None)
    _install_fake_httpx(monkeypatch)
    # Token retrieval should not be reached, but stub it to a sentinel
    # so a regression that DOES reach it is obvious in failure mode.
    _set_token(monkeypatch, "should-not-be-used")

    await trigger_race_postprocess(RACE_ID)

    assert _FakeAsyncClient.last_url is None, "HTTP must not be called"


async def test_noop_when_access_token_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_settings(
        monkeypatch,
        job="projects/x/locations/us-central1/jobs/race-postprocess",
    )
    _set_token(monkeypatch, None)
    _install_fake_httpx(monkeypatch)

    await trigger_race_postprocess(RACE_ID)

    assert _FakeAsyncClient.last_url is None


# ── trigger_race_postprocess: happy path + body shape ─────────────────


async def test_happy_path_posts_run_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    job = "projects/x/locations/us-central1/jobs/race-postprocess"
    _set_settings(monkeypatch, job=job)
    _set_token(monkeypatch, "fake-bearer-token")
    _install_fake_httpx(monkeypatch)

    await trigger_race_postprocess(RACE_ID)

    # URL: Cloud Run v2 Admin :run endpoint.
    assert _FakeAsyncClient.last_url == f"https://run.googleapis.com/v2/{job}:run"

    # Authorization header carries the ADC bearer token.
    assert _FakeAsyncClient.last_headers is not None
    assert _FakeAsyncClient.last_headers["Authorization"] == "Bearer fake-bearer-token"
    assert _FakeAsyncClient.last_headers["Content-Type"] == "application/json"

    # Body: containerOverrides.args has --race-id <uuid>, no --force.
    assert _FakeAsyncClient.last_json is not None
    overrides = _FakeAsyncClient.last_json["overrides"]
    container = overrides["containerOverrides"][0]
    assert container["args"] == ["--race-id", str(RACE_ID)]


async def test_force_flag_appended_to_args(monkeypatch: pytest.MonkeyPatch):
    _set_settings(
        monkeypatch,
        job="projects/x/locations/us-central1/jobs/race-postprocess",
    )
    _set_token(monkeypatch, "tok")
    _install_fake_httpx(monkeypatch)

    await trigger_race_postprocess(RACE_ID, force=True)

    container = _FakeAsyncClient.last_json["overrides"]["containerOverrides"][0]
    assert container["args"] == ["--race-id", str(RACE_ID), "--force"]


# ── trigger_race_postprocess: failure paths must not raise ────────────


async def test_swallows_http_4xx(monkeypatch: pytest.MonkeyPatch):
    _set_settings(
        monkeypatch,
        job="projects/x/locations/us-central1/jobs/race-postprocess",
    )
    _set_token(monkeypatch, "tok")
    _install_fake_httpx(monkeypatch)
    _FakeAsyncClient.next_response = _FakeResponse(403, "permission denied")

    # The contract is "never raises". If this raises the test fails.
    await trigger_race_postprocess(RACE_ID)


async def test_swallows_http_5xx(monkeypatch: pytest.MonkeyPatch):
    _set_settings(
        monkeypatch,
        job="projects/x/locations/us-central1/jobs/race-postprocess",
    )
    _set_token(monkeypatch, "tok")
    _install_fake_httpx(monkeypatch)
    _FakeAsyncClient.next_response = _FakeResponse(500, "internal error")

    await trigger_race_postprocess(RACE_ID)


async def test_swallows_network_exception(monkeypatch: pytest.MonkeyPatch):
    _set_settings(
        monkeypatch,
        job="projects/x/locations/us-central1/jobs/race-postprocess",
    )
    _set_token(monkeypatch, "tok")
    _install_fake_httpx(monkeypatch)
    _FakeAsyncClient.raise_exc = RuntimeError("connection reset")

    await trigger_race_postprocess(RACE_ID)


# ── _get_access_token: ImportError / ADC failure paths ────────────────
#
# These exercise the real _get_access_token (not the test-stubbed one)
# so we cover the failure handling rather than just the override hook.


def test_access_token_returns_none_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
):
    # Force `import google.auth` to raise. The function imports lazily
    # inside its body, so patching sys.modules works.
    import sys

    monkeypatch.setitem(sys.modules, "google.auth", None)
    # ``import google.auth`` against a None entry raises ImportError.
    result = jt_mod._get_access_token()
    assert result is None


def test_access_token_returns_none_on_adc_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """When google.auth IS importable but default() raises, return None.

    We don't want the test to depend on whether google-auth is actually
    installed in the venv. If it isn't, we install a fake module shape
    that exposes a default() that raises. If it IS installed, monkeypatch
    its default() to raise.
    """
    import sys
    import types

    # Build a minimal fake google.auth namespace if not present.
    google_auth = sys.modules.get("google.auth")
    if google_auth is None:
        fake_pkg = types.ModuleType("google")
        fake_auth = types.ModuleType("google.auth")
        fake_transport = types.ModuleType("google.auth.transport")
        fake_requests = types.ModuleType("google.auth.transport.requests")
        fake_requests.Request = type("Request", (), {})

        def boom(*a, **kw):
            raise RuntimeError("no credentials configured")

        fake_auth.default = boom
        monkeypatch.setitem(sys.modules, "google", fake_pkg)
        monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
        monkeypatch.setitem(sys.modules, "google.auth.transport", fake_transport)
        monkeypatch.setitem(
            sys.modules, "google.auth.transport.requests", fake_requests,
        )
    else:
        def boom(*a, **kw):
            raise RuntimeError("no credentials configured")

        monkeypatch.setattr(google_auth, "default", boom)

    assert jt_mod._get_access_token() is None
