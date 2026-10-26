"""OAuth desktop flow and Calendar API service factory."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"
TOKEN_META_PATH = PROJECT_ROOT / "token.meta.json"

DEFAULT_LOGIN_HINT = "moses.hoavo@gmail.com"
# Google Testing-mode refresh tokens expire after 7 days.
TESTING_REFRESH_DAYS = 7


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_token(creds: Credentials) -> None:
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    TOKEN_META_PATH.write_text(
        json.dumps(
            {
                "authorized_at": _utc_now().isoformat(),
                "account": DEFAULT_LOGIN_HINT,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _authorized_at() -> datetime | None:
    if not TOKEN_META_PATH.exists():
        return None
    try:
        raw = json.loads(TOKEN_META_PATH.read_text(encoding="utf-8"))
        value = raw.get("authorized_at")
        if not value:
            return None
        return datetime.fromisoformat(value)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _is_invalid_grant(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "invalid_grant" in text or "invalid_rapt" in text


def _interactive_auth(
    *,
    login_hint: str | None,
    open_browser: bool,
) -> Credentials:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_PATH.name}.\n"
            "1. Create a Google Cloud project and enable Google Calendar API.\n"
            "2. Configure OAuth consent (External) and add yourself as a test user.\n"
            "3. Create a Desktop OAuth client and download credentials.json\n"
            f"   into {PROJECT_ROOT}."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    # login_hint only pre-fills the email; it cannot switch Chrome profiles.
    # Default is paste-URL into the Moses profile (Work profile opens otherwise).
    auth_kwargs: dict = {
        "port": 0,
        "prompt": "consent",
        "access_type": "offline",
        "open_browser": open_browser,
    }
    if login_hint:
        auth_kwargs["login_hint"] = login_hint

    print(f"Target Google account: {login_hint}", flush=True)
    print(flush=True)
    if open_browser:
        print("Opening the default browser (often Work / mirus - avoid if possible).", flush=True)
        print(
            "Prefer: python -m src.cli auth --force  (paste URL in Moses Chrome)",
            flush=True,
        )
    else:
        print("1. Open your Moses Chrome profile (moses.hoavo@gmail.com), not Work.", flush=True)
        print("2. Copy/paste the URL printed below into that profile's address bar.", flush=True)
        print("3. Sign in / Allow, then return here.", flush=True)
        print(flush=True)
        auth_kwargs["authorization_prompt_message"] = (
            "Open this URL in your Moses Chrome profile:\n{url}\n"
        )

    sys.stdout.flush()
    creds = flow.run_local_server(**auth_kwargs)
    _write_token(creds)
    return creds


def get_credentials(
    *,
    force_consent: bool = False,
    login_hint: str | None = DEFAULT_LOGIN_HINT,
    open_browser: bool = False,
    interactive_on_failure: bool = True,
) -> Credentials:
    """Load or obtain OAuth credentials for the Calendar API.

    Access tokens (~1h) refresh from token.json automatically.
    Refresh tokens last until revoked, except OAuth apps still in Testing
    (Google expires them after ~7 days). On invalid_grant, start the
    paste-URL Moses-profile flow instead of crashing.
    """
    creds: Credentials | None = None

    if TOKEN_PATH.exists() and not force_consent:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.refresh_token and not force_consent:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError as exc:
            if not interactive_on_failure:
                raise
            print(
                f"Saved token refresh failed ({exc}). Starting Moses-profile login.",
                flush=True,
            )
            if _is_invalid_grant(exc):
                print(
                    "This is normal about every 7 days while the Cloud OAuth app "
                    "is still in Testing. Publishing the app to Production makes "
                    "refresh tokens last until you revoke them.",
                    flush=True,
                )
            creds = None

    if force_consent:
        print("Forcing a new login (existing token kept until this succeeds).", flush=True)

    return _interactive_auth(login_hint=login_hint, open_browser=open_browser)


def get_calendar_service(
    *,
    force_consent: bool = False,
    login_hint: str | None = DEFAULT_LOGIN_HINT,
    open_browser: bool = False,
    interactive_on_failure: bool = True,
):
    """Return an authorized Calendar API v3 service."""
    creds = get_credentials(
        force_consent=force_consent,
        login_hint=login_hint,
        open_browser=open_browser,
        interactive_on_failure=interactive_on_failure,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def token_info() -> dict | None:
    """Return a summary of the saved token, if any."""
    if not TOKEN_PATH.exists():
        return None
    data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    obtained = _authorized_at()
    age_days: float | None = None
    if obtained:
        age_days = (_utc_now() - obtained).total_seconds() / 86400
    return {
        "path": str(TOKEN_PATH),
        "has_refresh_token": bool(data.get("refresh_token")),
        "scopes": data.get("scopes") or SCOPES,
        "authorized_at": obtained.isoformat() if obtained else None,
        "age_days": age_days,
        "testing_refresh_likely_expired": (
            age_days is not None and age_days >= TESTING_REFRESH_DAYS
        ),
    }
