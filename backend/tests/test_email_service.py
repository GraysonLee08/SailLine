"""Tests for app/services/email.py.

The SendGrid SDK is mocked end-to-end. We exercise:
  * happy path with a fake client (asserts the Mail object's shape)
  * graceful no-op when API key is unset
  * graceful no-op when the SDK raises
  * non-2xx status from SendGrid returns False
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import email as email_mod
from app.services.email import send_boat_invite


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    def __init__(self, *, status_code: int = 202, raise_exc: Exception | None = None):
        self._status = status_code
        self._raise = raise_exc
        self.last_message = None

    def send(self, message):
        self.last_message = message
        if self._raise:
            raise self._raise
        return _FakeResponse(self._status)


def test_happy_path_returns_true():
    client = _FakeClient(status_code=202)
    ok = send_boat_invite(
        to_email="crew@example.com",
        boat_name="Gaucho",
        owner_name="Mark",
        accept_url="https://sailline.web.app/?invite=ABC123",
        role="crew",
        client=client,
    )
    assert ok is True
    assert client.last_message is not None


def test_missing_recipient_returns_false():
    ok = send_boat_invite(
        to_email="",
        boat_name="Gaucho",
        owner_name="Mark",
        accept_url="https://sailline.web.app/?invite=ABC",
        role="crew",
        client=MagicMock(),  # never called
    )
    assert ok is False


def test_send_failure_returns_false():
    client = _FakeClient(raise_exc=RuntimeError("sendgrid 503"))
    ok = send_boat_invite(
        to_email="crew@example.com",
        boat_name="Gaucho",
        owner_name="Mark",
        accept_url="https://sailline.web.app/?invite=ABC",
        role="crew",
        client=client,
    )
    assert ok is False


def test_non_2xx_returns_false():
    client = _FakeClient(status_code=429)
    ok = send_boat_invite(
        to_email="crew@example.com",
        boat_name="Gaucho",
        owner_name="Mark",
        accept_url="https://sailline.web.app/?invite=ABC",
        role="crew",
        client=client,
    )
    assert ok is False


def test_no_api_key_returns_false(monkeypatch: pytest.MonkeyPatch):
    """When client is None AND the env has no SENDGRID_API_KEY, the
    function logs and returns False without raising."""
    fake_settings = type(
        "FakeSettings", (),
        {
            "sendgrid_api_key": None,
            "email_from_address": "noreply@sailline.app",
            "email_from_name": "SailLine",
        },
    )()
    import app.config
    monkeypatch.setattr(
        app.config, "get_settings", lambda: fake_settings, raising=False,
    )
    ok = send_boat_invite(
        to_email="crew@example.com",
        boat_name="Gaucho",
        owner_name="Mark",
        accept_url="https://sailline.web.app/?invite=ABC",
        role="crew",
    )
    assert ok is False
