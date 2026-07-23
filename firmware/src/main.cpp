// ESP32 HUB75 64x64 album-art panel firmware.
//
// Responsibilities (deliberately minimal):
//   * Join WiFi, advertise over mDNS.
//   * Serve a tiny HTTP API:
//       POST /frame      body = PANEL_WIDTH*PANEL_HEIGHT*2 bytes, RGB565 big-endian
//       POST /brightness body = a single ASCII integer 0-255
//       POST /clear      blank the panel
//       GET  /           status / health
//   * Render whatever frame it is given.
//   * Accept OTA firmware updates, so you only ever flash over wires once.
//
// The host pod owns everything Spotify-related. Keeping the panel dumb means we
// never have to reflash to change behaviour.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <ESPAsyncWebServer.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

#include "config.h"

static const size_t FRAME_PIXELS = (size_t)PANEL_WIDTH * PANEL_HEIGHT;
static const size_t FRAME_BYTES  = FRAME_PIXELS * 2;  // RGB565

MatrixPanel_I2S_DMA *display = nullptr;
AsyncWebServer server(80);

// Frame handoff from the (async) network task to loop(). The web callback fills
// frameBuf and raises frameReady; loop() does the actual drawPixel work so we
// never touch the panel from the async TCP context.
static uint8_t  frameBuf[FRAME_BYTES];
static volatile bool frameReady = false;
static uint32_t framesDrawn = 0;

// ---------------------------------------------------------------------------

static void drawFrame() {
  size_t i = 0;
  for (int y = 0; y < PANEL_HEIGHT; y++) {
    for (int x = 0; x < PANEL_WIDTH; x++) {
      uint16_t color = (frameBuf[i] << 8) | frameBuf[i + 1];  // big-endian RGB565
      display->drawPixel(x, y, color);  // library treats uint16 as RGB565
      i += 2;
    }
  }
  framesDrawn++;
}

static void setupDisplay() {
  HUB75_I2S_CFG::i2s_pins pins = {
      R1_PIN, G1_PIN, B1_PIN, R2_PIN, G2_PIN, B2_PIN,
      A_PIN,  B_PIN,  C_PIN,  D_PIN,  E_PIN,
      LAT_PIN, OE_PIN, CLK_PIN};

  HUB75_I2S_CFG cfg(PANEL_WIDTH, PANEL_HEIGHT, PANEL_CHAIN, pins);
  cfg.clkphase = false;  // flip to true if every pixel looks shifted by one column

  display = new MatrixPanel_I2S_DMA(cfg);
  display->begin();
  display->setBrightness8(DEFAULT_BRIGHTNESS);
  display->clearScreen();
}

// A simple boot splash so you can tell the panel is alive before WiFi connects.
static void splash(const char *msg, uint16_t color) {
  display->clearScreen();
  display->setTextColor(color);
  display->setCursor(2, 2);
  display->print(msg);
}

static void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(LEDMATRIX_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("Connecting to WiFi");
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi failed; restarting in 5s");
    splash("WiFi X", display->color565(255, 0, 0));
    delay(5000);
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
    char body[160];
    snprintf(body, sizeof(body),
             "ledmatrix ok\nip=%s\nframes=%lu\nexpect_bytes=%u\n",
             WiFi.localIP().toString().c_str(),
             (unsigned long)framesDrawn, (unsigned)FRAME_BYTES);
    req->send(200, "text/plain", body);
  });

  // POST /frame: raw RGB565 body. Async bodies arrive in chunks, so we assemble
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
      [](AsyncWebServerRequest *req) { req->send(200, "text/plain", "ok"); },
      nullptr,
      [](AsyncWebServerRequest *req, uint8_t *data, size_t len, size_t index,
         size_t total) {
        char buf[8] = {0};
        size_t n = total < sizeof(buf) - 1 ? total : sizeof(buf) - 1;
        memcpy(buf, data, n);
        int v = constrain(atoi(buf), 0, MAX_BRIGHTNESS);  // never exceed ceiling
        display->setBrightness8(v);
      });

  server.on("/clear", HTTP_POST, [](AsyncWebServerRequest *req) {
    display->clearScreen();
    req->send(200, "text/plain", "ok");
  });

  server.begin();
  Serial.println("HTTP server started");
}

// OTA so that after the (painful) first wired flash, every later update goes
// over WiFi: `pio run -e esp32-matrix-ota -t upload`.
static void setupOTA() {
  ArduinoOTA.setHostname(LEDMATRIX_HOSTNAME);
#ifdef OTA_PASSWORD
  ArduinoOTA.setPassword(OTA_PASSWORD);
#endif
  ArduinoOTA.onStart([]() {
    // Stop pushing pixels during the update so DMA doesn't fight the flash write.
    display->clearScreen();
    display->setCursor(2, 2);
    display->print("OTA...");
  });
  ArduinoOTA.onEnd([]() { display->clearScreen(); });
  ArduinoOTA.begin();
  Serial.println("OTA ready");
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(200);
  setupDisplay();
  splash("boot", display->color565(0, 80, 255));
  setupWifi();
  setupOTA();
  splash("ready", display->color565(0, 200, 0));
  setupServer();
}

void loop() {
  ArduinoOTA.handle();
  if (frameReady) {
    frameReady = false;
    drawFrame();
  }
  delay(2);
}
