# Firmware — ESP32 HUB75 panel renderer

A minimal Arduino/PlatformIO firmware. Joins WiFi, advertises over mDNS, and
renders 64x64 RGB565 frames POSTed to it. No Spotify logic lives here.

## HTTP API

| Method | Path          | Body                                  | Effect               |
| ------ | ------------- | ------------------------------------- | -------------------- |
| GET    | `/`           | —                                     | status / health text |
| POST   | `/frame`      | 8192 bytes, RGB565 big-endian (64×64) | draw the frame       |
| POST   | `/brightness` | ASCII integer `0`–`255`               | set panel brightness |
| POST   | `/clear`      | —                                     | blank the panel      |

## Flashing hardware

This board has a **bare ESP32 and no USB-serial chip** (the USB-C is power-only).
Flash via the unpopulated 4-pin UART header (`3V3 / RX / TX / GND`) with a
USB-to-TTL adapter (CP2102/CH340) **set to 3.3V logic**. Wire it up:

```
adapter GND  -> header GND
adapter TXD  -> header RX     (crossover)
adapter RXD  -> header TX     (crossover)
adapter DTR  -> BOOT pad (IO0)   # for auto-reset (recommended)
adapter RTS  -> RST  pad (EN)    # for auto-reset (recommended)
```

Power the board from its **own USB-C**; leave the adapter's 3V3/5V pin off, and
share grounds (the GND wire does this). With DTR/RTS wired, `pio run -t upload`
auto-enters the bootloader. Without them, hold BOOT->GND, tap RST->GND, release
BOOT, then upload (and uncomment the manual-reset `upload_flags` in
`platformio.ini`).

**No soldering iron?** Solid-core wire (e.g. a single conductor from a Cat5
cable) press-fits into the plated header holes: strip ~10mm, push it in, and bend
it so it wedges against the hole wall. Wrap the other end around the adapter's
pins. It's flaky but fine for one flash, just hold it steady. For the bootloader,
pin one wire to GND and momentarily touch its tip to the BOOT then RST pads. You
only have to survive this **once** (see OTA below).

Back up the original clock firmware first, so this is reversible:

```bash
pip install esptool
esptool.py --port /dev/cu.usbserial-XXXX read_flash 0 0x400000 clock_backup.bin
```

## OTA: only flash over wires once

The firmware runs an OTA service. After the first wired flash succeeds and the
panel is on WiFi, every later update (including all the pinout tuning below) goes
over the air, no wires:

```bash
pio run -e esp32-matrix-ota -t upload
```

It targets `ledmatrix.local`. If mDNS doesn't resolve on your machine, set
`upload_port` to the panel's IP in the `esp32-matrix-ota` env in `platformio.ini`.

## Build & flash

Install [PlatformIO](https://platformio.org/install) (`pip install platformio`
or the VS Code extension), set `upload_port` in `platformio.ini` to your
adapter's port (`ls /dev/cu.*`), then:

```bash
cd firmware
cp src/config.h.example src/config.h     # then edit WiFi + (if needed) pins
pio run -t upload
pio device monitor                        # watch for "WiFi OK, IP: x.x.x.x"
```

On boot the panel shows `boot` then `ready`. Note the IP from the serial monitor
(and set a static DHCP lease for it on your router).

## Fixing a wrong pinout

The pins in `config.h` come from the **original ClockWise Plus firmware that
shipped on this board** (`yuan910715/clockwise`): library defaults, plus
`E_PIN = 18`, plus the **RBG green/blue swap** this panel needs (confirmed). So
the color bars from `python ../host/send_test.py <ip>` should be correct on the
first flash.

If something is still off:

- **Blue and green swapped** → revert to standard RGB: `B1_PIN 27`, `G1_PIN 26`,
  `B2_PIN 13`, `G2_PIN 12`.
- **Image split/scrambled** → check `E_PIN` (should be 18).
- **Every pixel shifted one column** → set `cfg.clkphase = true;` in `main.cpp`.

Re-test over OTA, no wires needed.

## Library

Uses [`ESP32-HUB75-MatrixPanel-I2S-DMA`](https://github.com/mrcodetastic/ESP32-HUB75-MatrixPanel-I2S-DMA)
for the panel and `ESPAsyncWebServer`/`AsyncTCP` for clean binary POST handling.
Versions are pinned in `platformio.ini`.
