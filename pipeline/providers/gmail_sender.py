#!/usr/bin/env python3
"""Gmail API sender for the C7 side rail.

This module is intentionally small and stdlib-only. It sends an already
approved first-touch email and returns Gmail ids so the same SQLite pipeline can
track provider, sent timestamp, reply thread, and follow-up calls.
"""

import base64
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".outreach" / "gmail-credentials.json"


def _request_json(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Gmail HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gmail network error: {exc.reason}") from exc


def _refresh_access_token():
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        creds = _load_client_credentials()
        client_id = client_id or creds.get("client_id", "")
        client_secret = client_secret or creds.get("client_secret", "")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        return ""
    form = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Gmail token refresh failed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gmail token refresh network error: {exc.reason}") from exc
    return data.get("access_token", "")


def _load_client_credentials(path=None):
    p = Path(path or os.environ.get("GMAIL_CREDENTIALS_JSON", "") or DEFAULT_CREDENTIALS_PATH)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    root = data.get("installed") or data.get("web") or data
    return {
        "client_id": (root.get("client_id") or "").strip(),
        "client_secret": (root.get("client_secret") or "").strip(),
    }


def access_token():
    token = os.environ.get("GMAIL_ACCESS_TOKEN", "").strip()
    return token or _refresh_access_token()


def send_email(to_email, subject, body, from_email="", reply_to=""):
    token = access_token()
    if not token:
        raise RuntimeError(
            "missing Gmail OAuth env: set GMAIL_ACCESS_TOKEN or "
            "GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN"
        )

    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    if from_email:
        msg["From"] = from_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    result = _request_json(
        "POST",
        SEND_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body={"raw": raw},
    )
    if not result.get("id"):
        raise RuntimeError(f"Gmail send returned no message id: {result}")
    return {
        "message_id": result.get("id", ""),
        "thread_id": result.get("threadId", ""),
    }
