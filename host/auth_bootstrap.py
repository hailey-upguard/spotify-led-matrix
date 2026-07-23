#!/usr/bin/env python3
"""One-time, run-locally OAuth to mint a long-lived Spotify refresh token.

The k8s pod is headless and can't do an interactive browser login, so we do the
Authorization Code flow once here on your laptop and print a refresh token. Drop
that token into your k8s secret and the pod will refresh access tokens forever.

Prereqs (in the Spotify developer dashboard, https://developer.spotify.com/dashboard):
  1. Create an app. Note its Client ID and Client Secret.
  2. Add this exact Redirect URI to the app:  http://127.0.0.1:8888/callback

Usage:
  export SPOTIFY_CLIENT_ID=...
  export SPOTIFY_CLIENT_SECRET=...
  python auth_bootstrap.py
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-currently-playing user-read-playback-state"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_received = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _received.update({k: v[0] for k, v in params.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h1>Done.</h1><p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, *_):
        pass  # quiet


def main():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first.", file=sys.stderr)
        sys.exit(2)

    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )
    auth_link = f"{AUTH_URL}?{query}"

    print("\nOpening your browser to authorize. If it doesn't open, visit:\n")
    print(auth_link, "\n")
    webbrowser.open(auth_link)

    # Catch the single callback.
    server = HTTPServer(("127.0.0.1", 8888), _Handler)
    while "code" not in _received and "error" not in _received:
        server.handle_request()

    if _received.get("error"):
        print("Authorization failed:", _received["error"], file=sys.stderr)
        sys.exit(1)
    if _received.get("state") != state:
        print("State mismatch; aborting.", file=sys.stderr)
        sys.exit(1)

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _received["code"],
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Authorization": f"Basic {basic}"},
        timeout=10,
    )
    resp.raise_for_status()
    tokens = resp.json()
    refresh = tokens.get("refresh_token")
    if not refresh:
        print("No refresh token returned:", tokens, file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS. Your refresh token (store this as a secret):\n")
    print(refresh)
    print("\nBase64 (for k8s secret stringData is fine, but if you use data:):")
    print(base64.b64encode(refresh.encode()).decode())
    print("=" * 60)


if __name__ == "__main__":
    main()
