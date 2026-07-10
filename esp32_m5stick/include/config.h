#pragma once

// Скопіюйте з secrets або задайте тут перед прошивкою
#ifndef WIFI_SSID
#define WIFI_SSID "YOUR_WIFI_SSID"
#endif

#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#endif

// Raw URL до dashboard/status.json у вашому GitHub репозиторії
#ifndef STATUS_URL
#define STATUS_URL "https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/dashboard/status.json"
#endif

// Інтервал оновлення (мс) — 15 с для швидкого, але легкого UI
#define REFRESH_MS 15000
