"""Main poll loop: Spotify now-playing -> 64x64 frame -> panel.

Designed to run forever as a k8s pod. Configuration is entirely via env vars so
the only mounted secret is the Spotify credentials.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from . import renderer
from .sender import PanelSender
from .spotify import SpotifyClient

log = logging.getLogger("spotify_matrix")


def _env(name: str, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing required env var %s", name)
        sys.exit(2)
    return val


class App:
    def __init__(self):
        self.spotify = SpotifyClient(
            client_id=_env("SPOTIFY_CLIENT_ID", required=True),
            client_secret=_env("SPOTIFY_CLIENT_SECRET", required=True),
            refresh_token=_env("SPOTIFY_REFRESH_TOKEN", required=True),
        )
        self.panel = PanelSender(_env("PANEL_HOST", required=True))
        self.poll_interval = float(_env("POLL_INTERVAL", "4"))
        self.brightness = float(_env("ART_BRIGHTNESS", "1.0"))
        # Caps average panel current so USB-C power can't brown out on bright art.
        self.power_limit = float(_env("POWER_LIMIT", "0.5"))
        # How long to keep the last cover up after music stops, before blanking.
        self.idle_timeout = float(_env("IDLE_TIMEOUT", "1800"))  # 30 min
        # The panel has no memory of what it was showing before it lost power, so
        # resend the current frame at this cadence even when nothing changed. Bounds
        # how long a panel that got power-cycled mid-song sits on its boot splash.
        self.repaint_interval = float(_env("REPAINT_INTERVAL", "60"))

        # State so we only re-render (not just re-send) when something changes.
        self._last_track_id = None
        self._last_blanked = False
        self._last_sent_frame = None  # cached bytes, so repaints skip refetch/render
        self._last_send_ts = None  # monotonic time of the last frame actually sent
        # monotonic timestamp of the last poll that saw music playing; None means
        # nothing has played since this process started.
        self._last_play_ts = None
        self._running = True

    def stop(self, *_):
        log.info("shutting down")
        self._running = False

    def _due_for_repaint(self, now):
        return self._last_send_ts is None or now - self._last_send_ts >= self.repaint_interval

    def _repaint(self, now):
        """Resend whatever should already be on screen, e.g. after a panel reboot."""
        frame = self._last_sent_frame if self._last_sent_frame is not None else renderer.black_frame()
        try:
            self.panel.send_frame(frame)
            self._last_send_ts = now
        except Exception as e:  # noqa: BLE001
            log.warning("failed to repaint panel: %s", e)

    def _blank(self, now):
        was_blanked = self._last_blanked
        if was_blanked and not self._due_for_repaint(now):
            return
        try:
            self.panel.send_frame(renderer.black_frame())
            self._last_blanked = True
            self._last_track_id = None
            self._last_sent_frame = None
            self._last_send_ts = now
            if not was_blanked:
                log.info("panel blanked")
        except Exception as e:  # noqa: BLE001
            log.warning("failed to blank panel: %s", e)

    def _show_track(self, np, now):
        same_track = np.track_id == self._last_track_id and not self._last_blanked
        if same_track and not self._due_for_repaint(now):
            return  # already on screen, no repaint due
        if not np.art_url:
            log.info("track '%s' has no art; blanking", np.title)
            self._blank(now)
            return
        try:
            # Repaint of an unchanged track: resend the cached frame instead of
            # refetching art and re-rendering it.
            frame = self._last_sent_frame if same_track else renderer.frame_from_url(
                np.art_url, brightness=self.brightness, power_limit=self.power_limit
            )
            self.panel.send_frame(frame)
            self._last_track_id = np.track_id
            self._last_blanked = False
            self._last_sent_frame = frame
            self._last_send_ts = now
            if not same_track:
                log.info("now showing: %s - %s", np.artist, np.title)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to render/send '%s': %s", np.title, e)

    def tick(self):
        try:
            np = self.spotify.now_playing()
        except Exception as e:  # noqa: BLE001
            log.warning("spotify poll failed: %s", e)
            return

        now = time.monotonic()

        # Music playing: show the cover and reset the idle clock.
        if np is not None and np.is_playing:
            self._last_play_ts = now
            self._show_track(np, now)
            return

        # Nothing playing. If we've never seen music this session, blank.
        if self._last_play_ts is None:
            self._blank(now)
            return

        # Music stopped: keep the last cover up until the idle timeout, then blank.
        if now - self._last_play_ts >= self.idle_timeout:
            self._blank(now)
        elif self._due_for_repaint(now):
            # Within the grace window: leave the last cover up, but resend it
            # periodically in case the panel itself got power-cycled meanwhile.
            self._repaint(now)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("started; polling every %ss", self.poll_interval)
        while self._running:
            self.tick()
            # Sleep in small steps so SIGTERM is responsive.
            slept = 0.0
            while self._running and slept < self.poll_interval:
                time.sleep(0.25)
                slept += 0.25


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    App().run()


if __name__ == "__main__":
    main()
