"""Sonos? No — Spotify → ESP32 HUB75 panel bridge.

Polls the Spotify Web API for the currently playing track, renders its album art
down to a 64x64 RGB565 frame, and pushes it to the panel firmware over HTTP.
"""

__all__ = ["spotify", "renderer", "sender", "main"]
