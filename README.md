# ledmatrix — Spotify album art on a 64x64 HUB75 panel

Drives an ESP32 + 64x64 HUB75 RGB LED panel to show pixelated album art of
whatever you're currently playing on Spotify.

## Hardware

I used [this](https://www.aliexpress.com/item/1005012414579321.html) panel and esp32 combo because it had a usb-serial adapter built in which is required to flash firmware.

## Architecture

```
Spotify Web API ──poll──► host pod (Python, k8s) ──HTTP POST 64x64 frame──► ESP32 ──HUB75──► panel
                          • OAuth + token refresh          (RGB888, 12 KB)   • renders frame
                          • download album art                               • nothing else
                          • resize 64x64 + pack RGB888
```

The **ESP32 firmware is deliberately dumb**: it joins WiFi and renders any 64x64
RGB888 frame POSTed to `/frame`. All Spotify logic, image downloading, and
resizing happen in the **host pod**, so you never reflash to change behaviour.
This also means no PSRAM is needed on the ESP32 (your board doesn't have it).

- [`firmware/`](firmware/) — PlatformIO/Arduino firmware for the ESP32.
- [`host/`](host/) — Python service that runs as a k8s pod.

## Quick start (the path through it)

1. **Flash the panel** — [`firmware/`](firmware/). Copy `config.h.example` →
   `config.h`, set WiFi, `pio run -t upload`, read the IP from the serial monitor.
2. **Smoke-test the panel** — from `host/`:
   `python send_test.py <panel-ip>` should draw color bars. This proves wiring +
   firmware before any Spotify work. If colors/layout are wrong, see
   [firmware/README.md](firmware/README.md#fixing-a-wrong-pinout).
3. **Get a Spotify refresh token** — create a Spotify app, then run
   `python host/auth_bootstrap.py` once locally (details in
   [host/README.md](host/README.md)).
4. **Deploy the host pod** — build the image, create the secret, set the panel
   IP in the deployment, `kubectl apply`. See [host/README.md](host/README.md).

Play something on Spotify and the panel updates within `POLL_INTERVAL` seconds.

## Notes / gotchas

- **Panel power:** a 64x64 panel can pull several amps at full white. Power it
  from a proper 5V supply via its dedicated power leads, not from the ESP32.
- **LAN reachability:** the pod must be able to reach the ESP32's IP on your LAN.
  On a home cluster (e.g. k3s on a box on the same network) this usually just
  works through the node; if you run NetworkPolicies, allow egress to the panel.
- **Static IP:** give the ESP32 a static DHCP lease. mDNS (`ledmatrix.local`)
  is handy on your laptop but generally won't resolve from inside the cluster.
- **You mentioned Sonos first, then Spotify** — this build targets Spotify. The
  firmware is source-agnostic, so a Sonos feeder pod could push frames the same
  way later if you want both.
