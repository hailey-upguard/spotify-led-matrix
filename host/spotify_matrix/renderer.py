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


_R_LEVELS = 31  # 5-bit channel
_G_LEVELS = 63  # 6-bit channel
_B_LEVELS = 31  # 5-bit channel


def _quantize(value: float, levels: int) -> tuple[int, float]:
    """Round `value` (0..255) to the nearest of `levels`+1 steps; return (level, its 0..255 value)."""
    step = 255.0 / levels
    level = max(0, min(levels, round(value / step)))
    return level, level * step


def _diffuse(err: list[float], next_err: list[float], x: int, e: float) -> None:
    """Floyd-Steinberg: push quantization error `e` onto the neighbors of pixel `x`."""
    err[x + 2] += e * 7 / 16
    next_err[x] += e * 3 / 16
    next_err[x + 1] += e * 5 / 16
    next_err[x + 2] += e * 1 / 16


def to_frame(
    img: Image.Image, brightness: float = 1.0, power_limit: float = 1.0
) -> bytes:
    """Resize to 64x64 and pack as big-endian RGB565.

    LANCZOS gives a clean downscale; the 64x64 grid itself supplies the
    "pixelated" look. `brightness` (0..1) is a flat pre-scale. `power_limit`
    (0..1) additionally dims only frames that would exceed the current budget,
    so a bright cover gets pulled down but a dark one is left alone.

    RGB565 only has 32/64/32 levels per channel, so naive rounding bands
    visibly on smooth gradients (album art skies, fades). Floyd-Steinberg
    error diffusion pushes each pixel's rounding error onto its neighbors,
    turning hard bands into fine dither noise the eye blends smooth.
    """
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    brightness = brightness * power_scale(img, power_limit)
    px = img.load()

    err_r, err_g, err_b = [0.0] * (WIDTH + 2), [0.0] * (WIDTH + 2), [0.0] * (WIDTH + 2)

    out = bytearray(FRAME_BYTES)
    i = 0
    for y in range(HEIGHT):
        next_err_r = [0.0] * (WIDTH + 2)
        next_err_g = [0.0] * (WIDTH + 2)
        next_err_b = [0.0] * (WIDTH + 2)
        for x in range(WIDTH):
            r, g, b = px[x, y]
            if brightness != 1.0:
                r *= brightness
                g *= brightness
                b *= brightness
            r += err_r[x + 1]
            g += err_g[x + 1]
            b += err_b[x + 1]

            r_level, r_used = _quantize(r, _R_LEVELS)
            g_level, g_used = _quantize(g, _G_LEVELS)
            b_level, b_used = _quantize(b, _B_LEVELS)

            _diffuse(err_r, next_err_r, x, r - r_used)
            _diffuse(err_g, next_err_g, x, g - g_used)
            _diffuse(err_b, next_err_b, x, b - b_used)

            rgb565 = (r_level << 11) | (g_level << 5) | b_level
            out[i] = (rgb565 >> 8) & 0xFF  # big-endian, matches firmware
            out[i + 1] = rgb565 & 0xFF
            i += 2
        err_r, err_g, err_b = next_err_r, next_err_g, next_err_b
    return bytes(out)


def black_frame() -> bytes:
    return bytes(FRAME_BYTES)


def frame_from_url(
    url: str, brightness: float = 1.0, power_limit: float = 1.0
) -> bytes:
    return to_frame(fetch_image(url), brightness=brightness, power_limit=power_limit)
