#!/usr/bin/env python3
"""Check whether current Gmail token can read mailbox messages."""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/home/outreach/.outreach/pipeline")
sys.path.insert(0, "workspace/pipeline")

from providers import gmail_sender  # noqa: E402


def main():
    token = gmail_sender.access_token()
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urllib.parse.urlencode(
        {"q": "from:mailer-daemon@googlemail.com newer_than:7d", "maxResults": 5}
    )
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"gmail_read_scope_ok= False")
        print(f"error= HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}")
        return 1
    print("gmail_read_scope_ok= True")
    print(f"messages_found= {len(data.get('messages', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
