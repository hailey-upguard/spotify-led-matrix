#!/usr/bin/env python3
"""Send a test frame to the panel to verify wiring/firmware before Spotify setup.

Usage:
  python send_test.py 192.168.1.42            # color-bar test pattern
  python send_test.py ledmatrix.local bars
  python send_test.py 192.168.1.42 image cover.jpg
  python send_test.py 192.168.1.42 clear

What to look for:
  * 'bars' draws R, G, B, white, gray vertical bands plus a 1px white border.
    - If red/blue are swapped, fix the R/B pin order in firmware/src/config.h.
    - If the image is split or duplicated top/bottom, fix E_PIN in config.h.
"""

import sys

from spotify_matrix import renderer
from spotify_matrix.sender import PanelSender


def bars_frame() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (64, 64))
    px = img.load()
    bands = [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 255),
        (128, 128, 128),
    ]
    for x in range(64):
        band = bands[min(x // 13, len(bands) - 1)]
        for y in range(64):
            px[x, y] = band
    # 1px white border to confirm full extent / no off-by-one cropping.
    for i in range(64):
        px[i, 0] = px[i, 63] = px[0, i] = px[63, i] = (255, 255, 255)
    return renderer.to_frame(img)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    host = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "bars"
    panel = PanelSender(host)

    if mode == "clear":
        panel.clear()
        print("cleared")
    elif mode == "image":
        path = sys.argv[3]
        from PIL import Image

        panel.send_frame(renderer.to_frame(Image.open(path).convert("RGB")))
        print(f"sent {path}")
    else:
        panel.send_frame(bars_frame())
        print("sent test bars")


if __name__ == "__main__":
    main()
