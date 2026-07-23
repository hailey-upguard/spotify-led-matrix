"""Turn an album-art URL into a 64x64 RGB565 frame the firmware can draw."""

from __future__ import annotations

import logging
from io import BytesIO

import requests
from PIL import Image, ImageStat

log = logging.getLogger(__name__)

WIDTH = 64
HEIGHT = 64
FRAME_BYTES = WIDTH * HEIGHT * 2

_session = requests.Session()


def fetch_image(url: str) -> Image.Image:
    resp = _session.get(url, timeout=10)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


def power_scale(img: Image.Image, power_limit: float) -> float:
    """Return a 0..1 factor that keeps panel current under budget.

    LED current is ~proportional to the average sub-pixel PWM duty across the
    whole panel. `power_limit` is the max allowed average duty (0..1) where 1.0
    means "allow full white everywhere" and e.g. 0.5 means "never draw more than
    ~half of full-white current". This matters because the panel is USB-C powered
    (possibly without PD), and full-coverage bright album art can otherwise pull
    more than the port supplies and brown the panel out.
    """
    if power_limit >= 1.0:
        return 1.0
    # Mean of (r+g+b)/(3*255) across all pixels = average duty cycle.
    stat = ImageStat.Stat(img)  # mean per channel, 0..255
    avg_duty = sum(stat.mean) / (3 * 255)
    if avg_duty <= power_limit or avg_duty == 0:
        return 1.0
    return power_limit / avg_duty


def to_frame(
    img: Image.Image, brightness: float = 1.0, power_limit: float = 1.0
) -> bytes:
    """Resize to 64x64 and pack as big-endian RGB565.

    LANCZOS gives a clean downscale; the 64x64 grid itself supplies the
    "pixelated" look. `brightness` (0..1) is a flat pre-scale. `power_limit`
    (0..1) additionally dims only frames that would exceed the current budget,
    so a bright cover gets pulled down but a dark one is left alone.
    """
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    brightness = brightness * power_scale(img, power_limit)
    px = img.load()

    out = bytearray(FRAME_BYTES)
    i = 0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y]
            if brightness != 1.0:
                r = int(r * brightness)
                g = int(g * brightness)
                b = int(b * brightness)
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i] = (rgb565 >> 8) & 0xFF  # big-endian, matches firmware
            out[i + 1] = rgb565 & 0xFF
            i += 2
    return bytes(out)


def black_frame() -> bytes:
    return bytes(FRAME_BYTES)


def frame_from_url(
    url: str, brightness: float = 1.0, power_limit: float = 1.0
) -> bytes:
    return to_frame(fetch_image(url), brightness=brightness, power_limit=power_limit)
