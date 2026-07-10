@echo off
chcp 65001 >nul
title ByteFlow Telegram Bot
cd /d "%~dp0"

echo ========================================
echo   ByteFlow Bot — запуск
echo ========================================
echo.

if not exist ".env" (
    echo [INFO] Файл .env не знайдено. Копіюю з .env.example...
    copy /Y ".env.example" ".env" >nul
    echo [WARN] Відредагуйте .env: BOT_TOKEN та ADMIN_ID
    echo.
)

echo [INFO] Перевірка залежностей...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Не вдалося встановити залежності. Перевірте Python.
    pause
    exit /b 1
)

echo [INFO] Запуск bot.py...
echo.
python bot.py

if errorlevel 1 (
    echo.
    echo [ERROR] Бот завершився з помилкою.
)

pause
