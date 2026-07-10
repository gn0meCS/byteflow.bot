#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "config.h"

static bool wifiReady = false;
static uint32_t lastFetch = 0;

struct DashData {
  String updatedAt = "-";
  int users = 0;
  int messages = 0;
  int orders = 0;
  int aiSessions = 0;
  int aiMessages = 0;
  int errors = 0;
  int activeAi = 0;
  float cpu = 0;
  float ram = 0;
  float procMb = 0;
  int uptime = 0;
  String status = "wait";
  String lastError = "-";
};

DashData dash;

void drawHeader() {
  M5.Display.fillScreen(BLACK);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(CYAN);
  M5.Display.setCursor(4, 4);
  M5.Display.println("ByteFlow Bot");
  M5.Display.drawLine(0, 16, M5.Display.width(), 16, DARKGREY);
}

void drawStatus(const String& line1, const String& line2 = "") {
  drawHeader();
  M5.Display.setTextColor(WHITE);
  M5.Display.setCursor(4, 24);
  M5.Display.println(line1);
  if (line2.length()) {
    M5.Display.setCursor(4, 40);
    M5.Display.println(line2);
  }
}

void connectWiFi() {
  if (wifiReady) return;
  drawStatus("WiFi...", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }
  wifiReady = (WiFi.status() == WL_CONNECTED);
}

bool fetchDashboard() {
  if (!wifiReady) return false;

  HTTPClient http;
  http.setTimeout(8000);
  http.begin(STATUS_URL);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    dash.status = "http_err";
    dash.lastError = "HTTP " + String(code);
    http.end();
    return false;
  }

  String payload = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    dash.status = "json_err";
    dash.lastError = err.c_str();
    return false;
  }

  dash.updatedAt = doc["updated_at"] | "-";
  dash.uptime = doc["uptime_sec"] | 0;
  dash.users = doc["today"]["users"] | 0;
  dash.messages = doc["today"]["messages"] | 0;
  dash.orders = doc["today"]["orders"] | 0;
  dash.aiSessions = doc["today"]["ai_sessions"] | 0;
  dash.aiMessages = doc["today"]["ai_messages"] | 0;
  dash.errors = doc["today"]["errors"] | 0;
  dash.activeAi = doc["live"]["active_ai_chats"] | 0;
  dash.cpu = doc["system"]["cpu_percent"] | 0.0f;
  dash.ram = doc["system"]["ram_percent"] | 0.0f;
  dash.procMb = doc["system"]["process_mb"] | 0.0f;
  dash.status = doc["status"] | "ok";

  if (doc["last_errors"].is<JsonArray>() && !doc["last_errors"].isNull()) {
    JsonArray arr = doc["last_errors"].as<JsonArray>();
    if (!arr.isNull() && arr.size() > 0) {
      JsonObject e = arr[0];
      String t = e["time"] | "";
      String m = e["message"] | "";
      dash.lastError = t + " " + m;
      if (dash.lastError.length() > 34) {
        dash.lastError = dash.lastError.substring(0, 33) + "...";
      }
    } else {
      dash.lastError = "-";
    }
  }

  return true;
}

void renderDashboard() {
  drawHeader();

  uint16_t statusColor = GREEN;
  if (dash.status == "error" || dash.errors > 0) statusColor = RED;
  else if (dash.status != "ok") statusColor = YELLOW;

  M5.Display.setTextColor(statusColor);
  M5.Display.setCursor(110, 4);
  M5.Display.print(dash.status);

  M5.Display.setTextColor(WHITE);
  M5.Display.setCursor(4, 22);
  M5.Display.printf("Upd: %s\n", dash.updatedAt.c_str());
  M5.Display.printf("Uptime: %dh %dm\n", dash.uptime / 3600, (dash.uptime % 3600) / 60);

  M5.Display.setTextColor(CYAN);
  M5.Display.println("--- Today ---");
  M5.Display.setTextColor(WHITE);
  M5.Display.printf("Users: %d  Msg: %d\n", dash.users, dash.messages);
  M5.Display.printf("Orders: %d  AI: %d\n", dash.orders, dash.aiSessions);
  M5.Display.printf("AI msg: %d Err: %d\n", dash.aiMessages, dash.errors);

  M5.Display.setTextColor(YELLOW);
  M5.Display.printf("Live AI chats: %d\n", dash.activeAi);

  M5.Display.setTextColor(DARKGREY);
  M5.Display.printf("CPU:%.0f%% RAM:%.0f%% Bot:%.0fMB\n", dash.cpu, dash.ram, dash.procMb);

  if (dash.errors > 0 || dash.status == "error") {
    M5.Display.setTextColor(RED);
    M5.Display.printf("ERR: %s\n", dash.lastError.c_str());
  }
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setBrightness(80);
  drawStatus("Boot...", "M5Stick ByteFlow");
  connectWiFi();
}

void loop() {
  M5.update();

  if (WiFi.status() != WL_CONNECTED) {
    wifiReady = false;
    connectWiFi();
  }

  if (millis() - lastFetch > REFRESH_MS) {
    lastFetch = millis();
    if (!fetchDashboard()) {
      drawStatus("Fetch error", dash.lastError);
      delay(1200);
    }
    renderDashboard();
  }

  delay(50);
}
