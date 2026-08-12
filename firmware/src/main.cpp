// ESP32 HUB75 64x64 album-art panel firmware.
//
// Responsibilities (deliberately minimal):
//   * Join WiFi, advertise over mDNS.
//   * Serve a tiny HTTP API:
//       POST /frame      body = PANEL_WIDTH*PANEL_HEIGHT*3 bytes, RGB888 (r,g,b per pixel)
//       POST /brightness body = a single ASCII integer 0-255
//       POST /clear      blank the panel
//       POST /panel      HUB75 timing + fade tuning (query params)
//       GET  /           status / health
//   * Render whatever frame it is given.
//
// Flashing is over USB only; there is deliberately no OTA (see firmware/README).
//
// The host pod owns everything Spotify-related. Keeping the panel dumb means we
// never have to reflash to change behaviour.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ESPAsyncWebServer.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>
#include <Preferences.h>

#include "config.h"

static const size_t FRAME_PIXELS = (size_t)PANEL_WIDTH * PANEL_HEIGHT;
static const size_t FRAME_BYTES  = FRAME_PIXELS * 3;  // RGB888

MatrixPanel_I2S_DMA *display = nullptr;
AsyncWebServer server(80);

// Frame handoff from the (async) network task to loop(). The web callback fills
// frameBuf and raises frameReady; loop() does the actual drawPixel work so we
// never touch the panel from the async TCP context.
static uint8_t  frameBuf[FRAME_BYTES];
static volatile bool frameReady = false;
static uint32_t framesDrawn = 0;

static uint32_t lastBrightnessStep = 0;
static uint8_t  currentBrightness = DEFAULT_BRIGHTNESS;
static uint8_t  targetBrightness = DEFAULT_BRIGHTNESS;

// Persisted in NVS so POST /panel can sweep these without a reflash. Everything
// except latch_blanking is only read by begin(), so changing it reboots.
static uint8_t  panelDriver = PANEL_DRIVER;
static bool     panelClkPhase = PANEL_CLKPHASE;
static uint8_t  panelLatBlank = PANEL_LATCH_BLANKING;
static uint8_t  panelDepth = PANEL_COLOR_DEPTH;
static uint8_t  panelPowerLimit = PANEL_POWER_LIMIT;
static uint8_t  panelI2sMhz = PANEL_I2S_MHZ;
// Estimated LED duty of the current frame, not its raw mean: the two differ by the
// panel's perceptual curve, and the power limit needs the former.
static uint8_t  frameDuty = 0;
static uint32_t rebootAt = 0;
static uint32_t wifiTestUntil = 0;

// fadePhase is linear 0-255 and gets squared on the way to the panel, so the fade
// reads as even rather than plunging then crawling.
enum FadeState : uint8_t { FADE_IDLE, FADE_OUT, FADE_IN };
static FadeState fadeState = FADE_IDLE;
static int16_t   fadePhase = 255;
static bool      framePending = false;  // waiting for the blackout to swap it in
static uint32_t  lastFrameHash = 0;
static uint32_t  lastFadeStep = 0;
static uint16_t  fadeMs = FADE_MS;

static void loadPanelCfg() {
  Preferences p;
  p.begin("panel", true);
  panelDriver   = p.getUChar("drv", PANEL_DRIVER);
  panelClkPhase = p.getBool("clk", PANEL_CLKPHASE);
  panelLatBlank = p.getUChar("latblk", PANEL_LATCH_BLANKING);
  panelDepth    = p.getUChar("depth", PANEL_COLOR_DEPTH);
  panelPowerLimit = p.getUChar("power", PANEL_POWER_LIMIT);
  panelI2sMhz   = p.getUChar("i2s", PANEL_I2S_MHZ);
  fadeMs        = p.getUShort("fadems", FADE_MS);
  p.end();
}

static void savePanelCfg() {
  Preferences p;
  p.begin("panel", false);
  p.putUChar("drv", panelDriver);
  p.putBool("clk", panelClkPhase);
  p.putUChar("latblk", panelLatBlank);
  p.putUChar("depth", panelDepth);
  p.putUChar("power", panelPowerLimit);
  p.putUChar("i2s", panelI2sMhz);
  p.putUShort("fadems", fadeMs);
  p.end();
}

static void clearPanelCfg() {
  Preferences p;
  p.begin("panel", false);
  p.clear();
  p.end();
}

// ---------------------------------------------------------------------------

// Sets panel brightness from the brightness target, the fade, and the power limit.
// The only place brightness reaches the panel, so the three cannot clobber one
// another.
static void applyBrightness() {
  const uint32_t fadeMul = (uint32_t)fadePhase * fadePhase / 255;  // gamma 2.0
  uint32_t out = (uint32_t)currentBrightness * fadeMul / 255;

  if (panelPowerLimit) {
    const uint32_t duty = (uint32_t)frameDuty * out / 255;
    if (duty > panelPowerLimit) out = out * panelPowerLimit / duty;
  }
  display->setBrightness8((uint8_t)out);
}

// FNV-1a of the incoming frame, used to skip transitions on unchanged covers.
static uint32_t frameHash() {
  uint32_t h = 2166136261u;
  for (size_t i = 0; i < FRAME_BYTES; i++) {
    h ^= frameBuf[i];
    h *= 16777619u;
  }
  return h;
}

static void drawFrame() {
  uint32_t sum = 0;
  for (size_t n = 0; n < FRAME_BYTES; n++) sum += frameBuf[n];

  // Linearised through the panel's perceptual curve; comparing a raw mean against
  // a duty limit overestimates current by 2-9x.
  const float m = (float)sum / (float)FRAME_BYTES / 255.0f;
  frameDuty = (uint8_t)(powf(m, 2.2f) * 255.0f + 0.5f);

  size_t i = 0;
  for (int y = 0; y < PANEL_HEIGHT; y++) {
    for (int x = 0; x < PANEL_WIDTH; x++) {
      display->drawPixelRGB888(x, y, frameBuf[i], frameBuf[i + 1], frameBuf[i + 2]);
      i += 3;
    }
  }
  framesDrawn++;
}

static void setupDisplay() {
  HUB75_I2S_CFG::i2s_pins pins = {
      R1_PIN, G1_PIN, B1_PIN, R2_PIN, G2_PIN, B2_PIN,
      A_PIN,  B_PIN,  C_PIN,  D_PIN,  E_PIN,
      LAT_PIN, OE_PIN, CLK_PIN};

  HUB75_I2S_CFG cfg(PANEL_WIDTH, PANEL_HEIGHT, PANEL_CHAIN, pins,
                    (HUB75_I2S_CFG::shift_driver)panelDriver);
  cfg.clkphase = panelClkPhase;
  cfg.latch_blanking = panelLatBlank;
  cfg.setPixelColorDepthBits(panelDepth);
  cfg.i2sspeed = panelI2sMhz >= 20 ? HUB75_I2S_CFG::HZ_20M
               : panelI2sMhz >= 16 ? HUB75_I2S_CFG::HZ_16M
                                   : HUB75_I2S_CFG::HZ_8M;

  display = new MatrixPanel_I2S_DMA(cfg);
  display->begin();
  display->setBrightness8(DEFAULT_BRIGHTNESS);
  display->clearScreen();
}

// Moves currentBrightness one proportional step towards targetBrightness.
static void stepBrightnessRamp() {
  if (currentBrightness == targetBrightness) return;
  int delta = (int)targetBrightness - (int)currentBrightness;
  int step = abs(delta) / BRIGHTNESS_RAMP_DIV;
  if (step < 1) step = 1;
  currentBrightness += (delta > 0) ? step : -step;
  applyBrightness();
}

// Advances a cover swap: ramp to black, swap the frame, ramp back up.
static void stepFade() {
  if (fadeState == FADE_IDLE) return;

  int step = (int)(255L * FADE_STEP_INTERVAL / (long)(fadeMs ? fadeMs : 1));
  if (step < 1) step = 1;

  if (fadeState == FADE_OUT) {
    fadePhase -= step;
    if (fadePhase <= 0) {
      fadePhase = 0;
      applyBrightness();  // dark before the content changes
      if (framePending) {
        drawFrame();
        framePending = false;
      }
      fadeState = FADE_IN;
      return;
    }
  } else {
    fadePhase += step;
    if (fadePhase >= 255) {
      fadePhase = 255;
      fadeState = FADE_IDLE;
    }
  }
  applyBrightness();
}

// Parses a /brightness body ("auto" or 0-255) into targetBrightness, reporting
// whether it understood the input.
static bool applyBrightnessCommand(const char *buf) {
  if (buf == nullptr || *buf == '\0') return false;

  // The host POSTs "auto" to undo a scheduled dim; there is no light sensor.
  if (strcmp(buf, "auto") == 0) {
    targetBrightness = DEFAULT_BRIGHTNESS;
    return true;
  }

  // Not atoi(): it returns 0 for junk, which reads as "brightness off".
  char *end = nullptr;
  long v = strtol(buf, &end, 10);
  if (end == buf) return false;
  while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') end++;
  if (*end != '\0') return false;

  targetBrightness = (uint8_t)constrain(v, 0L, (long)MAX_BRIGHTNESS);  // never exceed ceiling
  return true;
}

// Draws the wifi glyph at the given white level: two arcs and a chevron, cut to a
// +/-45 degree fan.
static void drawWifiOff(uint8_t level) {
  // Apex at y=46 so the glyph (reaching r=27.5 above it) spans y 18..46 and sits
  // vertically centred on the 64px panel.
  const float fanX = 31.5f, fanY = 46.0f;

  for (int y = 0; y < PANEL_HEIGHT; y++) {
    for (int x = 0; x < PANEL_WIDTH; x++) {
      const float dx = (float)x - fanX;
      const float dy = (float)y - fanY;
      const float r = sqrtf(dx * dx + dy * dy);
      bool on = false;

      const bool inFan = fabsf(dx) <= -dy;

      if (dy < 0.0f && inFan) {
        if ((r >= 12.0f && r <= 17.5f) || (r >= 21.0f && r <= 27.5f)) on = true;
      }
      // Same fan boundary as the arcs: anything wider breaks the diagonal their
      // ends are cut on, and the parts stop looking aligned.
      if (!on && r <= 8.5f && inFan) on = true;

      const uint8_t v = on ? level : 0;
      display->drawPixelRGB888(x, y, v, v, v);
    }
  }
}

// Breathes the glyph so it does not read as a frozen device.
static void pulseWifiOff(uint32_t ms) {
  const float phase = (float)(ms % 1600) / 1600.0f * 6.28319f;
  const uint8_t level = (uint8_t)(70.0f + 185.0f * (0.5f + 0.5f * sinf(phase)));
  drawWifiOff(level);
}

// Gaussian falloff around a ring of radius rc, so it does not alias into a jagged
// circle on a 64px grid.
static inline float ringWeight(float r, float rc) {
  const float d = r - rc;
  if (d < -7.0f || d > 7.0f) return 0.0f;
  return expf(-(d * d) / 6.0f);
}

// Boot animation: a chromatic ring expanding out of the centre, then fading. Each
// channel's ring sits at a slightly different radius so the band reads as colour.
static void bootIcon() {
  const uint32_t DUR = 1750;
  const uint32_t start = millis();
  uint32_t elapsed;

  while ((elapsed = millis() - start) < DUR) {
    const float t = (float)elapsed / (float)DUR;
    float grow = t / 0.68f;
    if (grow > 1.0f) grow = 1.0f;
    const float R = (1.0f - powf(1.0f - grow, 3.0f)) * 46.0f;  // easeOutCubic
    const float fade = t > 0.82f ? (1.0f - (t - 0.82f) / 0.18f) : 1.0f;

    for (int y = 0; y < PANEL_HEIGHT; y++) {
      for (int x = 0; x < PANEL_WIDTH; x++) {
        const float dx = (float)x - 31.5f;
        const float dy = (float)y - 31.5f;
        const float r = sqrtf(dx * dx + dy * dy);
        display->drawPixelRGB888(
            x, y,
            (uint8_t)(238.0f * ringWeight(r, R) * fade),
            (uint8_t)(198.0f * ringWeight(r, R - 2.2f) * fade),
            (uint8_t)(255.0f * ringWeight(r, R - 4.4f) * fade));
      }
    }
    delay(12);
  }
  display->clearScreen();
}

static void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(LEDMATRIX_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("Connecting to WiFi");
  // Held for the whole unassociated window, so a slow AP is distinguishable from a
  // dead device.
  const uint32_t start = millis();
  uint32_t lastDot = 0;
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    pulseWifiOff(millis() - start);
    if (millis() - lastDot >= 1000) {
      lastDot = millis();
      Serial.print(".");
    }
    delay(40);
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi failed; restarting in 5s");
    const uint32_t failedAt = millis();
    while (millis() - failedAt < 5000) {
      pulseWifiOff(millis() - start);
      delay(40);
    }
    ESP.restart();
  }

  Serial.print("WiFi OK, IP: ");
  Serial.println(WiFi.localIP());

  if (MDNS.begin(LEDMATRIX_HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("mDNS: http://%s.local\n", LEDMATRIX_HOSTNAME);
  }
}

static void setupServer() {
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
    char body[320];
    snprintf(body, sizeof(body),
             "ledmatrix ok\nip=%s\nframes=%lu\nexpect_bytes=%u\n"
             "brightness=%u\nbrightness_target=%u\nbrightness_max=%u\n"
             "panel_drv=%u\npanel_clkphase=%u\npanel_latblk=%u\n"
             "panel_depth=%u\npanel_refresh_hz=%d\npanel_i2s_mhz=%u\nfade_ms=%u\n"
             "power_limit=%u\nframe_mean=%u\n",
             WiFi.localIP().toString().c_str(),
             (unsigned long)framesDrawn, (unsigned)FRAME_BYTES,
             currentBrightness, targetBrightness, (unsigned)MAX_BRIGHTNESS,
             panelDriver, panelClkPhase ? 1 : 0, panelLatBlank, panelDepth,
             display->calculated_refresh_rate, panelI2sMhz, fadeMs, panelPowerLimit,
             frameDuty);
    req->send(200, "text/plain", body);
  });

  // POST /frame: raw RGB888 body. Async bodies arrive in chunks, so we assemble
  // by offset and only mark ready once the whole frame has landed.
  server.on(
      "/frame", HTTP_POST,
      [](AsyncWebServerRequest *req) {
        // onBody (below) handled the data; respond based on what we received.
        if (req->contentLength() == FRAME_BYTES) {
          frameReady = true;
          req->send(200, "text/plain", "ok");
        } else {
          req->send(400, "text/plain", "bad frame size");
        }
      },
      nullptr,
      [](AsyncWebServerRequest *req, uint8_t *data, size_t len, size_t index,
         size_t total) {
        if (total != FRAME_BYTES) return;  // ignore wrong-sized payloads
        if (index + len <= FRAME_BYTES) {
          memcpy(frameBuf + index, data, len);
        }
      });

  server.on(
      "/brightness", HTTP_POST,
      [](AsyncWebServerRequest *req) {
        // An urlencoded body never reaches onBody: ESPAsyncWebServer consumes it
        // as a POST param, and which half the text lands in depends on whether the
        // body contains an "=". Try both.
        if (req->params() == 1 && req->getParam(0)->isPost()) {
          const AsyncWebParameter *p = req->getParam(0);
          if (!applyBrightnessCommand(p->name().c_str())) {
            applyBrightnessCommand(p->value().c_str());
          }
        }
        req->send(200, "text/plain", "ok");
      },
      nullptr,
      [](AsyncWebServerRequest *req, uint8_t *data, size_t len, size_t index,
         size_t total) {
        // Sized off len, not total: a chunked body would read past `data`.
        if (index != 0) return;
        char buf[12] = {0};
        size_t n = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
        memcpy(buf, data, n);
        applyBrightnessCommand(buf);
      });

  server.on("/clear", HTTP_POST, [](AsyncWebServerRequest *req) {
    display->clearScreen();
    req->send(200, "text/plain", "ok");
  });

  // Query params rather than a body: the urlencoded body parsing above is exactly
  // what misparsed /brightness.
  server.on("/panel", HTTP_POST, [](AsyncWebServerRequest *req) {
    bool reboot = false;

    if (req->hasParam("reset")) {
      clearPanelCfg();
      req->send(200, "text/plain", "panel cfg cleared, rebooting to config.h defaults\n");
      rebootAt = millis() + 250;
      return;
    }

    if (req->hasParam("latblk")) {
      panelLatBlank = constrain(req->getParam("latblk")->value().toInt(), 1, 4);
      display->setLatBlanking(panelLatBlank);
      // setLatBlanking leaves its OE-bit recompute commented out upstream, so the
      // new blanking only takes hold once brightness is re-applied.
      display->setBrightness8(currentBrightness);
    }
    if (req->hasParam("drv")) {
      panelDriver = constrain(req->getParam("drv")->value().toInt(), 0, 5);
      reboot = true;  // only read by begin()
    }
    if (req->hasParam("clk")) {
      panelClkPhase = req->getParam("clk")->value().toInt() != 0;
      reboot = true;  // only read by begin()
    }
    if (req->hasParam("wifitest")) {
      const int secs = constrain(req->getParam("wifitest")->value().toInt(), 1, 60);
      wifiTestUntil = millis() + (uint32_t)secs * 1000;
    }
    if (req->hasParam("power")) {
      panelPowerLimit = constrain(req->getParam("power")->value().toInt(), 0, 255);
      applyBrightness();
    }
    if (req->hasParam("i2s")) {
      panelI2sMhz = constrain(req->getParam("i2s")->value().toInt(), 8, 20);
      reboot = true;
    }
    if (req->hasParam("depth")) {
      panelDepth = constrain(req->getParam("depth")->value().toInt(), 2, 12);
      reboot = true;
    }
    if (req->hasParam("fadems")) {
      fadeMs = constrain(req->getParam("fadems")->value().toInt(), 0, 3000);
    }
    savePanelCfg();

    char msg[160];
    snprintf(msg, sizeof(msg),
             "drv=%u clk=%u latblk=%u depth=%u power=%u fadems=%u%s\n",
             panelDriver, panelClkPhase ? 1 : 0, panelLatBlank, panelDepth,
             panelPowerLimit, fadeMs, reboot ? " (rebooting)" : "");
    req->send(200, "text/plain", msg);
    if (reboot) rebootAt = millis() + 250;
  });

  server.begin();
  Serial.println("HTTP server started");
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(200);
  // Printed before setupDisplay so a hang in DMA init is distinguishable from a
  // dead serial port.
  Serial.println("\nledmatrix: boot");
  loadPanelCfg();  // before setupDisplay: driver/clkphase are read by begin()
  Serial.printf("ledmatrix: panel drv=%u clkphase=%u latblk=%u depth=%u\n",
                panelDriver, panelClkPhase ? 1 : 0, panelLatBlank, panelDepth);
  setupDisplay();
  Serial.println("ledmatrix: display up");
  bootIcon();
  setupWifi();
  display->clearScreen();  // ready: blank until the host pushes a frame
  setupServer();
}

void loop() {
  if (wifiTestUntil) {
    if ((int32_t)(millis() - wifiTestUntil) < 0) {
      pulseWifiOff(millis());
      delay(40);
      return;
    }
    wifiTestUntil = 0;
    drawFrame();
    applyBrightness();
  }

  if (frameReady) {
    frameReady = false;
    const uint32_t h = frameHash();
    if (h != lastFrameHash) {
      lastFrameHash = h;
      if (framesDrawn == 0) {
        // Already black from the boot animation; nothing to fade out of.
        drawFrame();
        fadePhase = 0;
        fadeState = FADE_IN;
      } else {
        framePending = true;
        fadeState = FADE_OUT;
      }
    }
  }
  // Deferred: never restart from inside an async callback.
  if (rebootAt && millis() >= rebootAt) ESP.restart();

  uint32_t now = millis();
  if (now - lastFadeStep >= FADE_STEP_INTERVAL) {
    lastFadeStep = now;
    stepFade();
  }
  if (now - lastBrightnessStep >= BRIGHTNESS_STEP_INTERVAL) {
    lastBrightnessStep = now;
    stepBrightnessRamp();
  }
  delay(2);
}
