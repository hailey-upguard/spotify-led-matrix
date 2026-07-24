"""Turn an album-art URL into a 64x64 RGB888 frame the firmware can draw."""

from __future__ import annotations

import logging
from io import BytesIO

import requests
from PIL import Image, ImageFilter, ImageStat

log = logging.getLogger(__name__)

WIDTH = 64
HEIGHT = 64
FRAME_BYTES = WIDTH * HEIGHT * 3

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
    """Resize to 64x64 and pack as RGB888 (3 bytes/pixel, r,g,b).

    LANCZOS gives a clean downscale; the 64x64 grid itself supplies the
    "pixelated" look. `brightness` (0..1) is a flat pre-scale. `power_limit`
    (0..1) additionally dims only frames that would exceed the current budget,
    so a bright cover gets pulled down but a dark one is left alone.

    Deliberately no dithering here. RGB888 already gives 256 levels/channel,
    plenty for a 64px downscale, and this panel's LEDs are large and
    physically separated with no sub-pixel blending, so error-diffusion
    dithering doesn't disappear into a smooth average the way it would on a
    dense print or screen; it reads as visible colored speckle instead,
    especially in dark/near-black regions. Tried it twice (on RGB565, then
    again as a lighter pass here) and both times it made dark areas look
    worse, not smoother. Plain rounding avoids that outright.

    A source-res blur pass runs before the resize instead, sized to the
    downscale ratio. Album art (especially photographic covers) often has
    fine grain or small bright highlights that LANCZOS has no proper
    anti-alias prefilter for at large downscale ratios (source images can be
    10x+ larger than 64px); that grain folds into blocky patches on smooth
    gradients and speckle in dark regions on its own, with no dithering
    involved. Blurring first removes the noise before it gets sampled,
    rather than adding noise after quantization like dithering does, so it
    doesn't bring back the speckle problem above.

    A second, much smaller blur runs after the resize, at 64x64. This is a
    separate problem from the pre-blur: even with clean source noise, a
    smooth gradient sampled down to only 64 cells still steps from one flat
    LED color to the next with a hard edge between them. A ~0.2px blur here
    blends each cell into its neighbors, enough to take the hard edge off
    without softening logos/text; 0.4-1px were tried first and read as too
    soft on those.

    A median filter runs before all of that, on the source image. It
    targets a different kind of noise than the Gaussian blur above: isolated
    outlier pixels (JPEG block artefacts, sensor grain) rather than smooth
    gradients. This matters most on flat, near-black covers (e.g. a plain
    black background with a small logo), where a Gaussian blur alone smears
    each outlier into a soft, faint blob spread across several output
    cells instead of removing it; the median filter drops outliers before
    the blur has anything to smear.
    """
    scale = max(img.width / WIDTH, img.height / HEIGHT)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    if scale > 1:
        img = img.filter(ImageFilter.GaussianBlur(radius=scale / 4))
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.2))
    brightness = brightness * power_scale(img, power_limit)
    px = img.load()

    out = bytearray(FRAME_BYTES)
    i = 0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y]
            if brightness != 1.0:
                r = round(r * brightness)
                g = round(g * brightness)
                b = round(b * brightness)
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            i += 3
    return bytes(out)


def black_frame() -> bytes:
    return bytes(FRAME_BYTES)


def frame_from_url(
    url: str, brightness: float = 1.0, power_limit: float = 1.0
) -> bytes:
    return to_frame(fetch_image(url), brightness=brightness, power_limit=power_limit)
