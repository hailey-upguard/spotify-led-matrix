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
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import health, renderer
from .sender import PanelSender
from .spotify import SpotifyClient

log = logging.getLogger("spotify_matrix")

# datetime.weekday(): Monday=0 ... Sunday=6.
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_INDEX = {name: i for i, name in enumerate(_DAY_NAMES)}


def _env(name: str, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing required env var %s", name)
        sys.exit(2)
    return val


def _parse_days(spec: str) -> frozenset:
    """'wed-fri' -> {wed,thu,fri}; 'sat-mon' -> {sat,sun,mon} (wraps); 'wed' -> {wed}."""
    parts = spec.split("-")
    if len(parts) not in (1, 2):
        raise ValueError(f"bad day range {spec!r}")
    try:
        indices = [_DAY_INDEX[p] for p in parts]
    except KeyError as e:
        raise ValueError(f"unknown day name {e.args[0]!r} in {spec!r}") from None
    start, end = indices[0], indices[-1]
    days = set()
    i = start
    while True:
        days.add(i)
        if i == end:
            break
        i = (i + 1) % 7
    return frozenset(days)


def _parse_window(spec: str) -> tuple[dtime, dtime, str]:
    """Parse 'HH:MM-HH:MM:mode' (mode is 'off' or 'dim') into (start, end, mode)."""
    window, mode = spec.rsplit(":", 1)
    if mode not in ("off", "dim"):
        raise ValueError(f"mode must be 'off' or 'dim', got {mode!r}")
    start_str, end_str = window.split("-")
    return dtime.fromisoformat(start_str), dtime.fromisoformat(end_str), mode


def _parse_schedule(spec: str) -> list:
    """Parse 'wed-fri=18:30-04:30:off, sun-thu=23:30-08:45:off' into a list of
    (days, start, end, mode) entries, matched in order given."""
    entries = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        day_spec, _, window_spec = chunk.partition("=")
        days = _parse_days(day_spec.strip())
        start_t, end_t, mode = _parse_window(window_spec.strip())
        entries.append((days, start_t, end_t, mode))
    return entries


def _schedule_mode(now: datetime, entries: list) -> str | None:
    """Which schedule mode (if any) applies at `now`.

    Each entry is keyed by the weekday(s) the evening *starts* on (so a "Thu
    night" entry running past midnight still applies into Friday morning), so
    this checks both entries starting today and entries starting yesterday, in
    case they cross midnight. Returns the first matching entry, in the order
    given; overlapping entries are the caller's responsibility to avoid.
    """
    for days_ago in (0, 1):
        start_date = now.date() - timedelta(days=days_ago)
        for days, start_t, end_t, mode in entries:
            if start_date.weekday() not in days:
                continue
            start_dt = datetime.combine(start_date, start_t, tzinfo=now.tzinfo)
            end_date = start_date + timedelta(days=1) if end_t <= start_t else start_date
            end_dt = datetime.combine(end_date, end_t, tzinfo=now.tzinfo)
            if start_dt <= now < end_dt:
                return mode
    return None


class App:
    def __init__(self, health_state: health.HealthState | None = None):
        self.health = health_state or health.HealthState()
        self.spotify = SpotifyClient(
            client_id=_env("SPOTIFY_CLIENT_ID", required=True),
            client_secret=_env("SPOTIFY_CLIENT_SECRET", required=True),
            refresh_token=_env("SPOTIFY_REFRESH_TOKEN", required=True),
        )
        self.panel = PanelSender(_env("PANEL_HOST", required=True))
        self.poll_interval = float(_env("POLL_INTERVAL", "4"))
        # Dimming and current limiting both moved to the panel, which scales OE duty
        # instead of pixel values and so keeps all 256 levels. Kept only to warn.
        self.brightness = float(_env("ART_BRIGHTNESS", "1.0"))
        if self.brightness != 1.0:
            log.warning(
                "ART_BRIGHTNESS=%s ignored: set brightness on the panel instead "
                "(DEFAULT_BRIGHTNESS in firmware config.h, or POST /brightness)",
                self.brightness,
            )
            self.brightness = 1.0
        self.resample = renderer.resolve_resample(_env("RESAMPLE", "BICUBIC"))
        self.power_limit = float(_env("POWER_LIMIT", "1.0"))
        if self.power_limit != 1.0:
            log.warning(
                "POWER_LIMIT=%s ignored: the panel enforces its own limit now "
                "(PANEL_POWER_LIMIT in firmware config.h, or POST /panel?power=N)",
                self.power_limit,
            )
            self.power_limit = 1.0
        # How long to keep the last cover up after music stops, before blanking.
        self.idle_timeout = float(_env("IDLE_TIMEOUT", "1800"))  # 30 min
        # The panel has no memory of what it was showing before it lost power, so
        # resend the current frame at this cadence even when nothing changed. Bounds
        # how long a panel that got power-cycled mid-song sits on its boot splash.
        self.repaint_interval = float(_env("REPAINT_INTERVAL", "60"))

        # Quiet-hours schedule, e.g. "sun-thu=23:30-08:45:off, fri-sat=23:30-09:30:dim".
        # Any number of "<day>-<day>=HH:MM-HH:MM:mode" entries, comma-separated;
        # day ranges are arbitrary (e.g. "wed-fri") and keyed by the weekday the
        # evening *starts* on, so "Thursday night" running past midnight is still
        # a Thursday entry. TIMEZONE must be set correctly (the pod's system
        # clock is normally UTC) or these windows won't line up with your actual
        # evenings.
        self.timezone = ZoneInfo(_env("TIMEZONE", "UTC"))
        self.schedule_windows = self._parse_schedule_env("SCHEDULE")
        self.schedule_dim_brightness = int(_env("SCHEDULE_DIM_BRIGHTNESS", "60"))
        self._schedule_mode = None  # currently applied: None / "off" / "dim"

        # State so we only re-render (not just re-send) when something changes.
        self._last_track_id = None
        self._last_blanked = False
        self._last_sent_frame = None  # cached bytes, so repaints skip refetch/render
        self._last_send_ts = None  # monotonic time of the last frame actually sent
        # monotonic timestamp of the last poll that saw music playing; None means
        # nothing has played since this process started.
        self._last_play_ts = None
        self._running = True

        # Wedged = several poll intervals with no tick at all. The Spotify and
        # panel calls each have their own 5-10s timeout, so allow generous room
        # above the interval before calling the loop dead.
        self.health.configure(max(6 * self.poll_interval, 60.0))

    def stop(self, *_):
        log.info("shutting down")
        self._running = False

    def _parse_schedule_env(self, name):
        spec = _env(name, "")
        if not spec:
            return []
        try:
            return _parse_schedule(spec)
        except ValueError as e:
            log.error("invalid %s=%r: %s", name, spec, e)
            sys.exit(2)

    def _enter_schedule_mode(self, mode, now):
        if mode == "off":
            log.info("schedule: display off")
            self._blank(now)
        elif mode == "dim":
            log.info("schedule: ultra-dim (brightness=%d)", self.schedule_dim_brightness)
            try:
                self.panel.set_brightness(self.schedule_dim_brightness)
            except Exception as e:  # noqa: BLE001
                log.warning("failed to set scheduled dim brightness: %s", e)
        else:
            log.info("schedule: normal operation resumed")
            try:
                self.panel.set_auto_brightness()
            except Exception as e:  # noqa: BLE001
                log.warning("failed to restore auto brightness: %s", e)

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
                np.art_url, resample=self.resample
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
        now = time.monotonic()

        mode = _schedule_mode(datetime.now(self.timezone), self.schedule_windows)
        if mode != self._schedule_mode:
            self._enter_schedule_mode(mode, now)
            self._schedule_mode = mode
        if mode == "off":
            # Scheduled off overrides everything; don't poll Spotify or render
            # at all, just periodically re-assert blank in case the panel
            # itself got power-cycled during the quiet hours.
            if self._due_for_repaint(now):
                self._repaint(now)
            return

        try:
            np = self.spotify.now_playing()
        except Exception as e:  # noqa: BLE001
            log.warning("spotify poll failed: %s", e)
            return

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
            # Ready as soon as the first poll completes, however it went: a
            # Spotify or panel outage is logged and retried, not unready.
            self.health.tick()
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
    # Start the probe endpoint before App(), so a pod stuck on a slow first
    # Spotify token exchange still answers probes instead of looking dead.
    state = health.HealthState()
    health.start(state, int(os.environ.get("HEALTH_PORT", health.DEFAULT_PORT)))
    App(state).run()


if __name__ == "__main__":
    main()
