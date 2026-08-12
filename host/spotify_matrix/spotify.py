"""Minimal Spotify Web API client.

We deliberately avoid a heavyweight SDK on the hot path. The one-time OAuth dance
(getting a refresh token) is handled by auth_bootstrap.py; here we only ever
exchange that long-lived refresh token for short-lived access tokens and call the
"currently playing" endpoint.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

# Scopes the refresh token must have been granted (see auth_bootstrap.py).
SCOPES = "user-read-currently-playing user-read-playback-state"


@dataclass
class NowPlaying:
    track_id: str
    title: str
    artist: str
    art_url: Optional[str]
    is_playing: bool


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._session = requests.Session()

    # -- auth ---------------------------------------------------------------

    def _basic_auth(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return base64.b64encode(raw).decode()

    def _refresh_access_token(self) -> None:
        resp = self._session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            headers={"Authorization": f"Basic {self._basic_auth()}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        # Spotify may rotate the refresh token; keep the new one if provided.
        if data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
        # Refresh a minute early to avoid edge-of-expiry 401s.
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60
        log.info("refreshed Spotify access token")

    def _token(self) -> str:
        if self._access_token is None or time.time() >= self._expires_at:
            self._refresh_access_token()
        assert self._access_token is not None
        return self._access_token

    # -- api ----------------------------------------------------------------

    def now_playing(self) -> Optional[NowPlaying]:
        """Return the current track, or None if nothing is active.

        Spotify returns 204 (no content) when no device has anything loaded.
        """
        resp = self._session.get(
            NOW_PLAYING_URL,
            headers={"Authorization": f"Bearer {self._token()}"},
            params={"additional_types": "track,episode"},
            timeout=10,
        )

        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            # Token died early; force a refresh and retry once.
            self._access_token = None
            resp = self._session.get(
                NOW_PLAYING_URL,
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"additional_types": "track,episode"},
                timeout=10,
            )
        resp.raise_for_status()
        data = resp.json()

        item = data.get("item")
        if not item:
            return None

        is_playing = bool(data.get("is_playing"))

        # Tracks carry album.images; podcast episodes carry images directly.
        images = []
        if item.get("type") == "episode":
            images = item.get("images", []) or item.get("show", {}).get("images", [])
            artist = item.get("show", {}).get("name", "")
        else:
            images = item.get("album", {}).get("images", [])
            artists = item.get("artists", [])
            artist = ", ".join(a["name"] for a in artists) if artists else ""

        art_url = self._best_art(images)

        return NowPlaying(
            track_id=item.get("id") or item.get("uri", ""),
            title=item.get("name", ""),
            artist=artist,
            art_url=art_url,
            is_playing=is_playing,
        )

    @staticmethod
    def _best_art(images: list) -> Optional[str]:
        """Returns the URL of the largest image Spotify offers.

        Not the smallest-over-64px it used to pick: at 64x64 the renderer's resize
        is a no-op, so the thumbnail's jpeg ringing reached the LEDs 1:1. Downscaling
        from 640px averages ~100 source pixels per panel pixel and suppresses it,
        for one ~60KB fetch per track change.
        """
        if not images:
            return None
        sized = [im for im in images if im.get("width")]
        if not sized:
            return images[0].get("url")
        return max(sized, key=lambda im: im["width"])["url"]
