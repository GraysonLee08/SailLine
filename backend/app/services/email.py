"""Transactional email via SendGrid.

Pure I/O wrapper. Lazy SDK import — keeps tests + dev runs working
without the dep installed and without API keys configured. All
functions return a bool indicating "did the email go out"; callers
never see exceptions.

Why "best-effort": invite emails are convenience. The owner can
always fall back to copy-pasting the accept URL out of the boats
view. We don't want a SendGrid outage (or a missing API key in dev)
to break the invite flow.

The two callers are:
  * ``app/routers/crew.py`` — POST /api/boats/{id}/invites, when the
    owner attached an email to the invite.
  * Tests, with the SDK mocked.

Sender / from-address come from settings (``EMAIL_FROM_ADDRESS`` and
``EMAIL_FROM_NAME``). Domain setup (SPF/DKIM/DMARC) is operational
work tracked separately — until then expect emails to land in spam
for some recipients.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def send_boat_invite(
    *,
    to_email: str,
    boat_name: str,
    owner_name: str,
    accept_url: str,
    role: str,
    client=None,  # injected for tests
) -> bool:
    """Send a boat invite email. Returns True on success, False on
    any failure (no key, SDK missing, transient error).

    ``client`` is injected for tests; production callers leave it
    None and the function builds a fresh SendGrid client from
    settings.
    """
    if not to_email or not accept_url:
        log.warning(
            "email: missing recipient or accept_url; skipping send"
        )
        return False

    # Lazy import of settings keeps this module importable in test
    # environments where pydantic-settings can't construct.
    if client is None:
        try:
            from app.config import get_settings
            settings = get_settings()
        except Exception as e:  # noqa: BLE001
            log.warning("email: settings unavailable (%s)", e)
            return False
        if not settings.sendgrid_api_key:
            log.info(
                "email: SENDGRID_API_KEY not set; skipping send to %s "
                "(this is fine in dev — share accept_url manually)",
                to_email,
            )
            return False
        client = _build_client(settings.sendgrid_api_key)
        if client is None:
            return False
        from_email = settings.email_from_address
        from_name = settings.email_from_name
    else:
        # Test path — derive from-fields from settings if available
        # else use sane defaults.
        try:
            from app.config import get_settings
            settings = get_settings()
            from_email = settings.email_from_address
            from_name = settings.email_from_name
        except Exception:  # noqa: BLE001
            from_email = "noreply@sailline.app"
            from_name = "SailLine"

    subject = f"You've been invited to crew {boat_name}"
    html = _render_html(
        boat_name=boat_name,
        owner_name=owner_name,
        accept_url=accept_url,
        role=role,
    )
    plain = _render_plain(
        boat_name=boat_name,
        owner_name=owner_name,
        accept_url=accept_url,
        role=role,
    )

    try:
        from sendgrid.helpers.mail import (  # type: ignore[import-not-found]
            Mail, Email, To, Content,
        )
        message = Mail(
            from_email=Email(from_email, from_name),
            to_emails=To(to_email),
            subject=subject,
            plain_text_content=Content("text/plain", plain),
            html_content=Content("text/html", html),
        )
        response = client.send(message)
    except Exception as e:  # noqa: BLE001
        log.warning("email: SendGrid send failed (%s)", e)
        return False

    # SendGrid returns a Response with .status_code; 2xx = accepted.
    status_code = getattr(response, "status_code", None)
    if not (isinstance(status_code, int) and 200 <= status_code < 300):
        log.warning(
            "email: SendGrid returned non-2xx (status=%s)", status_code,
        )
        return False
    log.info("email: invite sent to %s (status=%s)", to_email, status_code)
    return True


def _build_client(api_key: str) -> Optional[object]:
    """Lazy SendGrid client construction. Returns None on import or
    init failure."""
    try:
        from sendgrid import SendGridAPIClient  # type: ignore[import-not-found]
        return SendGridAPIClient(api_key=api_key)
    except ImportError as e:
        log.warning("email: sendgrid not installed (%s)", e)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("email: failed to build SendGrid client (%s)", e)
        return None


def _render_html(
    *, boat_name: str, owner_name: str, accept_url: str, role: str,
) -> str:
    """Minimal inline-styled HTML — survives most email clients."""
    safe_boat = _escape(boat_name)
    safe_owner = _escape(owner_name)
    safe_role = _escape(role)
    safe_url = _escape(accept_url)
    return f"""\
<!doctype html>
<html>
  <body style="margin:0; padding:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f8f8f7; color:#16161a;">
    <div style="max-width:520px; margin:32px auto; padding:24px; background:white; border:1px solid #eaeaea; border-radius:12px;">
      <h1 style="font-size:20px; margin:0 0 16px;">You've been invited to crew {safe_boat}</h1>
      <p style="font-size:14px; line-height:1.55; margin:0 0 16px;">
        {safe_owner} has invited you to join <strong>{safe_boat}</strong> on SailLine as <strong>{safe_role}</strong>.
        You'll be able to see the boat's race plans, recorded tracks, and post-race stats.
      </p>
      <p style="margin:24px 0;">
        <a href="{safe_url}" style="display:inline-block; padding:12px 20px; background:#16161a; color:white; text-decoration:none; border-radius:8px; font-weight:500;">
          Accept invite
        </a>
      </p>
      <p style="font-size:12px; color:#6a6a6f; line-height:1.5; margin:0;">
        Or copy this link into your browser:<br>
        <span style="word-break:break-all;">{safe_url}</span>
      </p>
      <p style="font-size:11px; color:#9a9a9f; margin-top:24px;">
        Didn't expect this? You can safely ignore the email — the link only works for you.
      </p>
    </div>
  </body>
</html>
"""


def _render_plain(
    *, boat_name: str, owner_name: str, accept_url: str, role: str,
) -> str:
    return (
        f"{owner_name} has invited you to crew {boat_name} on SailLine "
        f"as {role}.\n\n"
        f"Accept the invite: {accept_url}\n\n"
        "Didn't expect this? You can safely ignore the email."
    )


def _escape(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
