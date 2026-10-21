// xiao-senses (Loci) — XIAO ESP32S3 Sense 当「眼睛和耳朵」，电脑上的中继当「大脑和嘴」。
//
// 按一下 BOOT 键：拍一张 JPEG，POST 到 RELAY_BASE/upload
// 按住 BOOT 键 ≥0.8 秒：录 3 秒音（16kHz 单声道 WAV），POST 到 RELAY_BASE/audio
// 浏览器打开 http://<板子IP>/            看状态（摄像头型号、对焦、内存）
//                 http://<板子IP>/capture     直接拍一张
//                 http://<板子IP>/listen?s=3  直接录几秒
//
// 编译：arduino-cli compile -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi .
// PSRAM 必须开 OPI，否则摄像头帧缓冲分配失败。
// OV5640 的自动对焦按 Seeed wiki xiao_esp32s3_camera_usage 例程初始化（出厂固件不做，不做就拍糊）。

#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiMulti.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>
#include "ESP32_OV5640_AF.h"

#define CAMERA_MODEL_XIAO_ESP32S3
#include "camera_pins.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

#define BUTTON_PIN 0      // BOOT 键，按下为 LOW
#define LED_PIN 21        // 板载黄灯，LOW 亮（和 SD 卡片选共用，所以本固件不用 SD 卡）
#define MIC_CLK 42
#define MIC_DATA 41
#define MIC_RATE 16000
#define LONG_PRESS_MS 800
#define RECORD_SECONDS 3
#define OV5640_AF_FOCUS_FRAMESIZE FRAMESIZE_SXGA
#define OV5640_FOCUS_TIMEOUT_MS 5000

WiFiMulti wifiMulti;
WebServer server(80);
I2SClass mic;
OV5640 ov5640 = OV5640();

const char *sensorName = "unknown";
bool afReady = false;
bool micReady = false;

void led(bool on) { digitalWrite(LED_PIN, on ? LOW : HIGH); }

void blink(int times, int ms) {
  for (int i = 0; i < times; i++) { led(true); delay(ms); led(false); delay(ms); }
}

bool waitForOv5640Focus() {
  const uint32_t started = millis();
  uint8_t status = 0;
  while (millis() - started < OV5640_FOCUS_TIMEOUT_MS) {
    status = ov5640.getFWStatus();
    if (status == 0x10) return true;  // FOCUSED
    delay(100);
  }
  Serial.printf("OV5640 focus not settled, last FW_STATUS=0x%02X\n", status);
  return false;
}

bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_SVGA;  // 800x600，够看清牌匾，上传约几十 KB
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  if (!psramFound()) {
    Serial.println("ERROR: 没有 PSRAM。编译时要加 PSRAM=opi");
    return false;
  }
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("ERROR: camera init failed 0x%x（0x105 = 摄像头没认到，检查排线和扩展板）\n", err);
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  switch (s->id.PID) {
    case OV5640_PID: sensorName = "OV5640"; break;
    case OV3660_PID: sensorName = "OV3660"; break;
    case OV2640_PID: sensorName = "OV2640"; break;
    default: sensorName = "other"; break;
  }
  Serial.printf("camera ok: %s (PID 0x%04X)\n", sensorName, s->id.PID);

  if (s->id.PID == OV3660_PID) {  // 例程里 OV3660 默认画面翻转、偏暗
    s->set_vflip(s, 1);
    s->set_brightness(s, 1);
  }

  if (s->id.PID == OV5640_PID) {
    if (s->set_framesize(s, OV5640_AF_FOCUS_FRAMESIZE) != 0) {
      Serial.println("WARN: cannot set SXGA for AF focus");
    } else if (!ov5640.start(s)) {
      Serial.println("WARN: OV5640 CHIPID check failed, AF skipped");
    } else {
      int r1 = ov5640.focusInit();
      int r2 = (r1 == 0) ? ov5640.autoFocusMode() : -1;
      afReady = (r1 == 0 && r2 == 0 && waitForOv5640Focus());
      Serial.printf("OV5640 AF: focusInit=%d autoFocusMode=%d ready=%d\n", r1, r2, afReady);
    }
    s->set_framesize(s, config.frame_size);
  }
  return true;
}

bool initMic() {
  mic.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!mic.begin(I2S_MODE_PDM_RX, MIC_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("ERROR: mic init failed");
    return false;
  }
  return true;
}

camera_fb_t *grabFresh() {
  // 丢掉缓冲里的旧帧，拿按键之后的那一帧
  camera_fb_t *fb = esp_camera_fb_get();
  if (fb) esp_camera_fb_return(fb);
  return esp_camera_fb_get();
}

int postBytes(const char *path, const char *contentType, const uint8_t *data, size_t len) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  HTTPClient http;
  String url = String(RELAY_BASE) + path;
  http.begin(url);
  http.setTimeout(15000);
  http.addHeader("Content-Type", contentType);
  http.addHeader("X-Device", DEVICE_ID);
  int code = http.POST((uint8_t *)data, len);
  Serial.printf("POST %s %u bytes -> %d\n", url.c_str(), (unsigned)len, code);
  http.end();
  return code;
}

void snapAndSend() {
  led(true);
  camera_fb_t *fb = grabFresh();
  if (!fb) { Serial.println("ERROR: capture failed"); led(false); blink(5, 60); return; }
  int code = postBytes("/upload", "image/jpeg", fb->buf, fb->len);
  esp_camera_fb_return(fb);
  led(false);
  if (code == 200) blink(1, 150); else blink(3, 80);
}

void recordAndSend() {
  if (!micReady) { blink(5, 60); return; }
  led(true);
  size_t wavSize = 0;
  uint8_t *wav = mic.recordWAV(RECORD_SECONDS, &wavSize);
  led(false);
  if (!wav) { Serial.println("ERROR: record failed"); blink(5, 60); return; }
  int code = postBytes("/audio", "audio/wav", wav, wavSize);
  free(wav);
  if (code == 200) blink(2, 150); else blink(3, 80);
}

void handleStatus() {
  String json = "{";
  json += "\"device\":\"" DEVICE_ID "\",";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  json += "\"ssid\":\"" + WiFi.SSID() + "\",";
  json += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  json += "\"camera\":\"" + String(sensorName) + "\",";
  json += "\"af_ready\":" + String(afReady ? "true" : "false") + ",";
  json += "\"mic\":" + String(micReady ? "true" : "false") + ",";
  json += "\"psram_free\":" + String(ESP.getFreePsram()) + ",";
  json += "\"relay\":\"" RELAY_BASE "\"}";
  server.send(200, "application/json", json);
}

void handleCapture() {
  camera_fb_t *fb = grabFresh();
  if (!fb) { server.send(500, "text/plain", "capture failed"); return; }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send_P(200, "image/jpeg", (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

void handleListen() {
  if (!micReady) { server.send(500, "text/plain", "mic not ready"); return; }
  int secs = server.hasArg("s") ? constrain(server.arg("s").toInt(), 1, 10) : RECORD_SECONDS;
  size_t wavSize = 0;
  uint8_t *wav = mic.recordWAV(secs, &wavSize);
  if (!wav) { server.send(500, "text/plain", "record failed"); return; }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send_P(200, "audio/wav", (const char *)wav, wavSize);
  free(wav);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  if (strlen(WIFI_SSID_1)) wifiMulti.addAP(WIFI_SSID_1, WIFI_PASS_1);
  if (strlen(WIFI_SSID_2)) wifiMulti.addAP(WIFI_SSID_2, WIFI_PASS_2);
  Serial.print("Wi-Fi");
  for (int i = 0; i < 40 && wifiMulti.run() != WL_CONNECTED; i++) { Serial.print("."); led(i % 2); delay(250); }
  led(false);
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi ok: %s  IP %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
    MDNS.begin("senses");  // 同网段可试 http://senses.local/（会场 Wi-Fi 常常不通，用 IP）
  } else {
    Serial.println("\nWARN: Wi-Fi not connected (只支持 2.4GHz；iPhone 热点开「最大兼容性」)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);  // 不等串口，脱离电脑也能跑
  pinMode(LED_PIN, OUTPUT);
  led(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  bool camOk = initCamera();
  micReady = initMic();
  connectWifi();

  server.on("/", handleStatus);
  server.on("/capture", handleCapture);
  server.on("/listen", handleListen);
  server.begin();

  Serial.printf("ready: camera=%d mic=%d\n", camOk, micReady);
  blink(camOk ? 2 : 6, 120);
}

void loop() {
  server.handleClient();
  if (WiFi.status() != WL_CONNECTED) wifiMulti.run();

  if (digitalRead(BUTTON_PIN) == LOW) {
    uint32_t t0 = millis();
    while (digitalRead(BUTTON_PIN) == LOW && millis() - t0 < LONG_PRESS_MS) delay(10);
    if (millis() - t0 >= LONG_PRESS_MS) {
      recordAndSend();
    } else {
      snapAndSend();
    }
    while (digitalRead(BUTTON_PIN) == LOW) delay(10);  // 等松手
  }
}
