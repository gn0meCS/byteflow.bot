# Прошивка M5Stick — ByteFlow Dashboard

## Що показує екран
- Статус бота (ok / error)
- Час оновлення та uptime
- Користувачі, повідомлення, замовлення за сьогодні
- AI-сесії та помилки
- CPU / RAM / пам'ять процесу бота
- Остання помилка (якщо є)

## Налаштування

1. Відредагуйте `include/config.h`:
   - `WIFI_SSID`
   - `WIFI_PASSWORD`
   - `STATUS_URL` — raw URL до `dashboard/status.json` у GitHub

2. Зберіть і прошийте через PlatformIO:
   ```bash
   cd esp32_m5stick
   pio run -t upload
   ```

## GitHub

Бот оновлює `dashboard/status.json` локально і (за наявності `GITHUB_TOKEN`) пушить у репозиторій.
M5Stick читає файл через raw.githubusercontent.com кожні 15 секунд.

## Змінні .env на сервері бота

```env
GITHUB_TOKEN=ghp_...
GITHUB_REPO=your_user/byteflow-bot
GITHUB_STATS_PATH=dashboard/status.json
DASHBOARD_UPDATE_INTERVAL=30
```
