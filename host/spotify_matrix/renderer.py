"""Turn an album-art URL into a 64x64 RGB888 frame the firmware can draw."""

from __future__ import annotations

import logging
from io import BytesIO

import requests
from PIL import Image

log = logging.getLogger(__name__)

WIDTH = 64
HEIGHT = 64
FRAME_BYTES = WIDTH * HEIGHT * 3

_session = requests.Session()

# LANCZOS is deliberately not the default: its negative lobes overshoot at
# high-contrast edges, lighting pixels the source has as pure black. On a mostly
# black cover it left 306 such pixels against BICUBIC's 59 and BOX's 40. BOX is
# the cleanest but softens thin strokes.
RESAMPLE_FILTERS = {
    "LANCZOS": Image.LANCZOS,
    "BICUBIC": Image.BICUBIC,
    "BILINEAR": Image.BILINEAR,
    "HAMMING": Image.HAMMING,
    "BOX": Image.BOX,
}
DEFAULT_RESAMPLE = Image.BICUBIC


def resolve_resample(name: str | None):
    """Maps a RESAMPLE env value to a PIL filter, warning and defaulting if unknown."""
    if not name:
        return DEFAULT_RESAMPLE
    key = name.strip().upper()
    if key in RESAMPLE_FILTERS:
        return RESAMPLE_FILTERS[key]
    log.warning(
        "unknown RESAMPLE %r, expected one of %s; using BICUBIC",
        name,
        ", ".join(RESAMPLE_FILTERS),
    )
    return DEFAULT_RESAMPLE


def fetch_image(url: str) -> Image.Image:
    resp = _session.get(url, timeout=10)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


def to_frame(img: Image.Image, resample=None) -> bytes:
    """Resizes to 64x64 and packs as RGB888.

    No brightness or current scaling here: both live on the panel, where they scale
    OE duty rather than pixel values and so cost no colour resolution.

    Deliberately no dithering or blur either. Both were tried and made dark areas
    worse on this panel, and the lit-black-pixel artefacts they were masking turned
    out to be the downscale's own ringing (see RESAMPLE_FILTERS).
    """
    img = img.resize((WIDTH, HEIGHT), resample or DEFAULT_RESAMPLE)
    px = img.load()

    out = bytearray(FRAME_BYTES)
    i = 0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y]
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            i += 3
    return bytes(out)


def black_frame() -> bytes:
    return bytes(FRAME_BYTES)


def frame_from_url(url: str, resample=None) -> bytes:
    return to_frame(fetch_image(url), resample=resample)
