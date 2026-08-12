# Firmware — ESP32 HUB75 panel renderer

A minimal Arduino/PlatformIO firmware. Joins WiFi, advertises over mDNS, and
renders 64x64 RGB888 frames POSTed to it. No Spotify logic lives here.

## HTTP API

| Method | Path          | Body                                            | Effect                                            |
| ------ | ------------- | ----------------------------------------------- | ------------------------------------------------- |
| GET    | `/`           | —                                               | status / health text                              |
| POST   | `/frame`      | 12288 bytes, RGB888 (r,g,b per pixel, 64×64)    | draw the frame                                    |
| POST   | `/brightness` | ASCII integer `0`–`255`, or `auto`              | ramp to that brightness; `auto` = back to default |
| POST   | `/clear`      | —                                               | blank the panel                                   |
| POST   | `/panel`      | query params, see [Panel timing](#panel-timing) | HUB75 timing + fade, persisted in NVS             |

## Panel timing

Ghosting (pixels lit that should be off, and blacks reading green) is a HUB75
timing problem, and the right values are a property of the **panel**, not the
board — so they do not survive a hardware swap. Tunable at runtime, persisted in
NVS, and reported by `GET /`:

```bash
P=http://ledmatrix.local
curl -X POST "$P/panel?latblk=3"    # latch blanking 1-4, applies live
curl -X POST "$P/panel?clk=0"       # clock phase, reboots (~4s)
curl -X POST "$P/panel?drv=2"       # shift driver, reboots
curl -X POST "$P/panel?depth=8"     # colour depth 2-12, reboots
curl -X POST "$P/panel?power=102"   # avg duty ceiling 0-255, live
curl -X POST "$P/panel?fadems=220"  # cover fade, per direction
curl -X POST "$P/panel?wifitest=20" # preview the no-wifi glyph for N seconds
curl -X POST "$P/panel?reset=1"     # drop NVS, back to config.h
```

Settle on values here, then copy them into `config.h` so a fresh flash keeps
them. What the current panel needed:

- **`latch_blanking = 2`** (library minimum is 1, which is what the old board
  used). At 1 this panel ghosted badly and never reached a true black. This was
  the whole fix.
- **`clkphase = false`**, even though the library defaults to `true`. With `true`
  the image shifts one pixel right and column 0 wraps to the far edge.
- **`drv = 0`** (plain shift register). If a future panel ghosts and no
  `latblk` value cleans it up, try `drv=2` — FM6126A/ICN2038S chips need an init
  sequence, and without it they ghost and never black out properly.

## Cover transitions

A new cover fades down to black and back up rather than cutting, `FADE_MS` per
direction. This is in the firmware, not the host, because a host-side fade would
mean streaming a dozen interpolated 12KB frames per transition over WiFi — the
same link that is already too marginal to finish an OTA. Ramping the panel's own
brightness costs no bandwidth at all.

Two details worth knowing:

- Frames are hashed (FNV-1a) and an incoming frame identical to what is already
  displayed skips the transition. The host re-pushes the current cover on
  reconnect and after a panel reboot, and fading out and back into the same image
  reads as a glitch.
- `frames` in `GET /` only increments at the _bottom_ of the fade, since that is
  when the swap happens. Polling right after a POST can race it.

The fade multiplier and the brightness target are composed in one place
(`applyBrightness`), so a scheduled dim landing mid-transition cannot fight it.

RGB888 was chosen over RGB565 specifically to avoid gradient banding: 565 only
gives 32/64/32 levels per channel, which bands visibly on smooth album art
(skies, fades). The panel's own PWM colour engine natively drives 8 bits per
channel (`drawPixelRGB888`), so sending full precision from the host avoids
the banding at the source instead of dithering around it.

## Brightness

There is **no auto-brightness**, because the MatrixPortal S3 has no ambient
light sensor. The panel boots at `DEFAULT_BRIGHTNESS` and only changes when
something POSTs `/brightness`.

Day/night dimming lives in the host instead, which already does it on a schedule
(`SCHEDULE_DIM_BRIGHTNESS` + quiet hours). `auto` is kept as an accepted body
value meaning "back to `DEFAULT_BRIGHTNESS`", since that is how the host undoes a
scheduled dim.

Changes ramp rather than snap: each tick moves `1/BRIGHTNESS_RAMP_DIV` of the
remaining distance, so a scheduled dim fades in instead of stepping. `GET /`
reports `brightness` (where the panel is now), `brightness_target` (where it is
heading), and `brightness_max`.

> Worth knowing if you port this back to an ESP32 with a real sensor: Adafruit's
> arduino-esp32 variant header for this board defines `PIN_LIGHTSENSOR A5`
> (GPIO 5), but nothing is connected to it. Reading it returns a floating ADC pin
> that drifts over roughly 112–390 regardless of room light, which mapped onto
> brightness looks exactly like a hypersensitive, jumpy auto-brightness stuck at
> the dim end. CircuitPython's board definition for this board lists no light
> sensor and no A5, which is the reliable signal.

## Flashing hardware

The board is an **Adafruit MatrixPortal ESP32-S3**. The S3 has a USB-Serial/JTAG
peripheral built into the silicon, so the USB-C port is all you need: no TTL
adapter, no wires, no soldering.

```bash
pio run -e esp32-matrix -t upload
```

esptool drives EN/IO0 over the USB control lines and enters the bootloader by
itself.

> **Do NOT hold the BOOT button.** Holding BOOT (GPIO0 low) at power-on forces
> the ROM into _download mode_, and download mode never starts your application.
> The flash still writes and still verifies, so this fails silently and looks
> exactly like broken firmware: no serial output, no WiFi, a dark panel. If you
> suspect it, `esptool.py --before no_reset --no-stub flash_id` answering
> "Staying in bootloader" straight after a hard reset confirms it. The fix is a
> plain power-cycle (or a tap of RESET) with BOOT untouched.
>
> The older instructions here described a BOOT->GND / RST->GND dance. That was
> for the previous all-in-one ClockWise Plus board, which had a bare ESP32 and no
> USB-serial chip. It does not apply to this board and will actively break it.

## No OTA

There is deliberately no over-the-air update path. There was one (`ArduinoOTA`
plus an `espota` env), and it was removed: it would die partway through an upload
and then stop answering on port 3232 until the panel was rebooted, so every flash
ended up going over USB anyway. The likely cause was `ArduinoOTA.handle()`
competing with the async web server and `drawFrame()` in `loop()` while the host
kept pushing 12KB frames, made worse by marginal WiFi.

USB flashing is ~10 seconds and needs no button presses on this board, so OTA was
carrying risk without buying much. Anything that genuinely needs changing at
runtime is exposed over HTTP instead — see [Panel timing](#panel-timing).

## Build & flash

Install [PlatformIO](https://platformio.org/install) (`pip install platformio`
or the VS Code extension), plug in the USB-C cable, then:

```bash
cd firmware
cp src/config.h.example src/config.h     # then edit WiFi + (if needed) pins
pio run -t upload
pio device monitor                        # watch for "WiFi OK, IP: x.x.x.x"
```

On boot the panel shows `boot` then `ready`. Note the IP from the serial monitor
(and set a static DHCP lease for it on your router).

## Fixing a wrong pinout

The pins in `config.h` are Adafruit's **fixed HUB75 socket wiring** for the
MatrixPortal ESP32-S3, taken from their own `MTX_*` board definitions, so they are
not a guess and should not need changing. The color bars from
`python ../host/send_test.py <ip>` should be correct on the first flash.

If something is off, it is a property of the panel you plugged in, not the board:

- **Green and blue swapped** → your panel is RBG rather than RGB. Swap `G1_PIN`
  with `B1_PIN` (41 ↔ 40) and `G2_PIN` with `B2_PIN` (39 ↔ 37).
- **Image split or duplicated top/bottom** → check `E_PIN` (21). A 64x64 is 1/32
  scan and needs the E address line.
- **Every pixel shifted one column** → `PANEL_CLKPHASE`, see
  [Panel timing](#panel-timing). It must be `false` on the current panel.
- **Pixels lit that should be dark** → `PANEL_LATCH_BLANKING`, same section.

Re-flash over USB, about 10 seconds.

## Library

Uses [`ESP32-HUB75-MatrixPanel-I2S-DMA`](https://github.com/mrcodetastic/ESP32-HUB75-MatrixPanel-I2S-DMA)
for the panel and `ESPAsyncWebServer`/`AsyncTCP` for clean binary POST handling.
Versions are pinned in `platformio.ini`.
