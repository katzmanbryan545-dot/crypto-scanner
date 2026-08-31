# 🚀 Деплой бота на Render.com (Бесплатный план)

## Подготовка завершена ✅

- ✅ Health check сервер добавлен (aiohttp на порту PORT)
- ✅ requirements.txt обновлён со всеми зависимостями
- ✅ Код отправлен на GitHub

## Шаги для деплоя на Render.com

### 1. Создание Web Service

1. Зайди на [render.com](https://render.com) и войди/зарегистрируйся
2. Нажми **"New +"** → **"Web Service"**
3. Подключи свой GitHub репозиторий: `katzmanbryan545-dot/crypto-scanner`
4. Выбери ветку: `main`

### 2. Настройка сервиса

**Name:** `crypto-scanner-bot` (или любое имя)

**Region:** `Frankfurt (EU Central)` (ближайший регион)

**Branch:** `main`

**Runtime:** `Python 3`

**Build Command:**
```bash
pip install -r requirements.txt
```

**Start Command:**
```bash
python main.py
```

**Instance Type:** `Free` (бесплатный план)

### 3. Переменные окружения (Environment Variables)

Добавь следующие переменные через интерфейс Render:

```
TELEGRAM_TOKEN=<твой токен бота>
OPENROUTER_API_KEY=<твой ключ OpenRouter>
TAVILY_API_KEY=<твой ключ Tavily>
GOOGLE_SHEETS_ID=<ID твоей Google таблицы>
MY_TELEGRAM_ID=<твой Telegram ID>
PORT=8080
```

**⚠️ ВАЖНО:** Render автоматически устанавливает PORT, но можно указать явно.

### 4. Google Sheets credentials

Для работы с Google Sheets нужен `credentials.json`:

**Вариант 1 - Через файл (рекомендуется):**
1. В Render Dashboard → Environment → Add Secret File
2. Имя файла: `credentials.json`
3. Содержимое: скопируй весь JSON из твоего локального credentials.json

**Вариант 2 - Через переменную окружения:**
```
GOOGLE_CREDENTIALS=<весь JSON credentials в одну строку>
```
И обнови код для чтения из переменной окружения.

### 5. Health Check настройки

Render автоматически настроит health checks:
- **Path:** `/`
- **Expected Status:** `200`
- **Response:** `{"status": "ok"}`

### 6. Деплой

Нажми **"Create Web Service"** и жди завершения деплоя (2-3 минуты).

## После деплоя

✅ **Проверка работы:**
- Бот должен быть онлайн в Telegram
- Health check URL: `https://crypto-scanner-bot.onrender.com/`
- Логи доступны в Dashboard → Logs

⚠️ **Ограничения бесплатного плана:**
- Сервис засыпает после 15 минут неактивности
- 750 часов в месяц (достаточно для одного сервиса 24/7)
- Первый запрос после сна может занять 30-50 секунд

## Решение проблем

### Бот не отвечает после деплоя
- Проверь логи в Render Dashboard
- Убедись что все environment variables установлены
- Проверь что credentials.json загружен как Secret File

### Health check fails
- Убедись что PORT из переменной окружения (не хардкод)
- Проверь что aiohttp установлен в requirements.txt

### Ошибки с Google Sheets
- Убедись что credentials.json корректно загружен
- Проверь что у Service Account есть доступ к таблице

## Альтернатива: Railway.app

Если Render не подходит, можно использовать [Railway.app](https://railway.app):
- Тот же процесс подключения GitHub
- Переменные окружения настраиваются аналогично
- $5 бесплатных кредитов в месяц

## Мониторинг

Для предотвращения сна используй UptimeRobot:
1. Зарегистрируйся на [uptimerobot.com](https://uptimerobot.com)
2. Добавь monitor: `https://crypto-scanner-bot.onrender.com/`
3. Interval: 5 минут
4. Health check будет пинговать бота и не даст ему заснуть
