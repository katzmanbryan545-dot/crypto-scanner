import os
import json
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import requests
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from openai import OpenAI
from tavily import TavilyClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКА КЛЮЧЕЙ И ТАБЛИЦЫ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
MY_TELEGRAM_ID = os.getenv("MY_TELEGRAM_ID")

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, TAVILY_API_KEY, GOOGLE_SHEETS_ID, MY_TELEGRAM_ID]):
    raise ValueError("Отсутствуют обязательные переменные окружения: TELEGRAM_TOKEN, OPENAI_API_KEY, TAVILY_API_KEY, GOOGLE_SHEETS_ID, MY_TELEGRAM_ID")

ADMIN_CHAT_ID = int(MY_TELEGRAM_ID)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Кэш алертов волатильности: {coin_name: last_alert_timestamp}
volatility_alerts_cache = {}

# Функция чтения проектов и цен покупки из Google Таблицы
def get_projects_from_sheet():
    try:
        url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv"
        df = pd.read_csv(url)

        projects_list = []
        for _, row in df.iterrows():
            name = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if name:
                # Безопасно считываем цену покупки из 4-й колонки (индекс 3)
                buy_price = None
                if len(row) > 3 and pd.notna(row.iloc[3]):
                    try:
                        buy_price = float(str(row.iloc[3]).replace("$", "").replace(",", ".").strip())
                    except ValueError:
                        pass

                projects_list.append({"name": name, "buy_price": buy_price})
        return projects_list
    except Exception as e:
        print(f"Ошибка чтения таблицы: {e}")
        return []

# Функция получения текущей цены с CoinGecko API
def get_current_crypto_price(project_name):
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": project_name.lower(), "vs_currencies": "usd", "include_24hr_change": "true"}
        response = requests.get(url, params=params, timeout=5).json()

        if project_name.lower() in response:
            price = response[project_name.lower()]["usd"]
            change = response[project_name.lower()]["usd_24h_change"]
            return price, change
        return None, None
    except Exception:
        return None, None

# Функция поиска новостей через Tavily
def search_news_tavily(project_name):
    try:
        tavily = TavilyClient(api_key=TAVILY_API_KEY)
        search_query = f"{project_name} crypto airdrop claim tge mainnet snapshot news 2026"
        search_result = tavily.search(query=search_query, max_results=5, topic="news")
        return search_result
    except Exception as e:
        print(f"Ошибка поиска новостей для {project_name}: {e}")
        return None

# Функция анализа новостей через OpenAI
def analyze_with_openai(project_name, search_result):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        # Формируем текст с сохранением HTML-ссылок
        news_text = ""
        if search_result and "results" in search_result:
            for item in search_result["results"]:
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("content", "")
                news_text += f"Заголовок: {title}\nURL: {url}\nОписание: {snippet}\n\n"

        prompt = f"""
        Ты - жесткий и лаконичный крипто-аналитик. Твоя задача - отфильтровать мусор и выдать инвестору СВЕЖИЕ ФАКТЫ по проекту {project_name}.
        Игнорируй: маркетинг, мемы, АМА-сессии, партнерства, общие рассуждения.
        Ищи и выделяй ТОЛЬКО критические триггеры: Клейм (Claim), Дроп (Airdrop/Snapshot), Дедлайны (Deadline), запуск Майннета (Mainnet), формы KYC/Sybil.

        Если по проекту идет обычное затишье и критических новостей/дедлайнов нет, ответь ОДНИМ словом: "Тишина".

        ВАЖНО: Если находишь важные новости, ОБЯЗАТЕЛЬНО включи кликабельные HTML-ссылки в формате <a href="URL">текст</a> на первоисточники.
        Пиши строго без воды. Используй только обычные дефисы вместо длинных тире.

        Данные из сети:
        {news_text}
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Ошибка анализа {project_name}: {str(e)}"

# Функция анализа новостей проекта (обертка для async)
def analyze_project_news(project_name):
    search_result = search_news_tavily(project_name)
    if not search_result:
        return "Ошибка поиска новостей"
    return analyze_with_openai(project_name, search_result)

# --- МОДУЛЬ MARKET PULSE (РЫНОЧНЫЙ ПУЛЬС) ---
def get_fear_greed_index():
    """Получает индекс страха и жадности с API"""
    try:
        url = "https://api.alternative.me/fng/"
        response = requests.get(url, timeout=5).json()

        if "data" in response and len(response["data"]) > 0:
            data = response["data"][0]
            value = int(data.get("value", 0))
            classification = data.get("value_classification", "Unknown")
            timestamp = data.get("timestamp", "")

            return {
                "value": value,
                "classification": classification,
                "timestamp": timestamp
            }
        return None
    except Exception as e:
        print(f"Ошибка получения Fear & Greed Index: {e}")
        return None

def get_btc_price():
    """Получает текущую цену BTC и изменение за 24ч"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"}
        response = requests.get(url, params=params, timeout=5).json()

        if "bitcoin" in response:
            price = response["bitcoin"]["usd"]
            change = response["bitcoin"]["usd_24h_change"]
            return price, change
        return None, None
    except Exception as e:
        print(f"Ошибка получения цены BTC: {e}")
        return None, None

async def get_market_pulse_text():
    """Формирует текст рыночного пульса"""
    loop = asyncio.get_event_loop()

    # Получаем индекс страха/жадности
    fng_data = await loop.run_in_executor(None, get_fear_greed_index)

    # Получаем цену BTC
    btc_price, btc_change = await loop.run_in_executor(None, get_btc_price)

    if not fng_data:
        return "🌡 <b>Рыночный пульс:</b> данные недоступны"

    # Эмодзи для статуса
    status_emoji = {
        "Extreme Fear": "😱",
        "Fear": "😰",
        "Neutral": "😐",
        "Greed": "😏",
        "Extreme Greed": "🤑"
    }

    emoji = status_emoji.get(fng_data["classification"], "📊")

    pulse_text = (
        f"🌡 <b>Рыночный пульс:</b>\n"
        f"{emoji} Fear & Greed Index: <b>{fng_data['value']}/100</b> ({fng_data['classification']})\n"
    )

    if btc_price and btc_change is not None:
        change_str = f"+{btc_change:.1f}%" if btc_change >= 0 else f"{btc_change:.1f}%"
        pulse_text += f"₿ Bitcoin: <b>${btc_price:,.0f}</b> ({change_str} за 24ч)"

    return pulse_text

# Функция поиска причины волатильности через Tavily и OpenAI
def search_volatility_reason(project_name):
    """Ищет причину резкого изменения цены токена"""
    try:
        tavily = TavilyClient(api_key=TAVILY_API_KEY)
        search_query = f"{project_name} token crypto why price surge dump news"
        search_result = tavily.search(query=search_query, max_results=5, topic="news")

        if not search_result or "results" not in search_result:
            return "Не удалось найти свежие новости о причине движения цены."

        # Формируем текст для анализа
        news_text = ""
        for item in search_result["results"]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("content", "")
            news_text += f"Заголовок: {title}\nURL: {url}\nОписание: {snippet}\n\n"

        # Анализируем через OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""
        Ты - крипто-аналитик. Объясни в 2-3 предложениях, почему резко изменилась цена токена {project_name}.

        Ищи конкретные триггеры: листинг на бирже, важные новости, взлом, регуляторные решения, действия китов, технические проблемы.

        Если причина не ясна из новостей, так и скажи честно.
        Включай HTML-ссылки на источники: <a href="URL">текст</a>

        Данные:
        {news_text}
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        return f"Ошибка поиска: {str(e)}"

# --- МОДУЛЬ АЛЕРТОВ ВОЛАТИЛЬНОСТИ ---
async def check_volatility_alerts():
    """Проверяет волатильность монет каждые 15 минут и отправляет алерты при |change_24h| >= 15%"""
    loop = asyncio.get_event_loop()

    projects_data = await loop.run_in_executor(None, get_projects_from_sheet)
    if not projects_data:
        print("Не удалось загрузить проекты для проверки волатильности")
        return

    now = datetime.now()

    for project in projects_data:
        coin_name = project["name"]

        # Получаем текущую цену и изменение за 24 часа
        curr_price, change24h = await loop.run_in_executor(None, get_current_crypto_price, coin_name)

        if curr_price is None or change24h is None:
            continue

        # Проверяем порог волатильности
        if abs(change24h) >= 15.0:
            # Проверяем кэш: отправляли ли алерт по этой монете за последние 6 часов
            last_alert_time = volatility_alerts_cache.get(coin_name)

            if last_alert_time and (now - last_alert_time) < timedelta(hours=6):
                print(f"Алерт для {coin_name} пропущен (отправлен менее 6 часов назад)")
                continue

            # Формируем сообщение
            direction = "🚀 ВЗЛЕТ" if change24h > 0 else "📉 ДАМП"
            alert_text = (
                f"{direction}\n\n"
                f"Монета: <b>{coin_name.upper()}</b>\n"
                f"Изменение за 24ч: <b>{change24h:+.2f}%</b>\n"
                f"Текущая цена: <b>${curr_price:.6f}</b>\n\n"
                f"⚠️ Высокая волатильность!"
            )

            # Добавляем инлайн-кнопку для исследования причины
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Узнать причину (ИИ-ресерч)", callback_data=f"why_{coin_name}")]
            ])

            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=alert_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                # Обновляем кэш
                volatility_alerts_cache[coin_name] = now
                print(f"Алерт отправлен для {coin_name}: {change24h:+.2f}%")
            except Exception as e:
                print(f"Ошибка отправки алерта для {coin_name}: {e}")

        await asyncio.sleep(1)

# Главная функция утреннего дайджеста
async def send_daily_digest():
    # Получаем рыночный пульс для шапки
    pulse_text = await get_market_pulse_text()

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"{pulse_text}\n\n🔍 Начинаю аудит портфолио и поиск крипто-новостей...",
        parse_mode="HTML"
    )

    loop = asyncio.get_event_loop()
    projects_data = await loop.run_in_executor(None, get_projects_from_sheet)

    if not projects_data:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text="❌ Не удалось загрузить данные из Google Таблицы.")
        return

    important_updates = []
    prices_text = "📊 <b>Текущие курсы твоих монет:</b>\n\n"
    has_prices = False
    silent_projects_count = 0

    for p in projects_data:
        name = p["name"]
        buy_price = p["buy_price"]

        # 1. Проверяем цену монеты на бирже (async wrapper)
        curr_price, change24h = await loop.run_in_executor(None, get_current_crypto_price, name)
        if curr_price:
            has_prices = True
            change_str = f"+{change24h:.1f}%" if change24h >= 0 else f"{change24h:.1f}%"
            prices_text += f"▪️ <b>{name.upper()}</b>: ${curr_price} ({change_str})"

            # Если в таблице указана твоя цена покупки - считаем чистый профит
            if buy_price:
                roi = ((curr_price - buy_price) / buy_price) * 100
                roi_str = f"+{roi:.1f}% 🟢" if roi >= 0 else f"{roi:.1f}% 🔴"
                prices_text += f" | Твой вход: ${buy_price} ({roi_str})"
            prices_text += "\n"

        # 2. Ищем важные новости через ИИ (async wrapper)
        report = await loop.run_in_executor(None, analyze_project_news, name)
        if report.lower() == "тишина":
            silent_projects_count += 1
        else:
            important_updates.append(f"🔥 <b>{name}</b>:\n{report}")

        await asyncio.sleep(2)

    # Отправляем блок цен (если нашли торгующиеся токены)
    if has_prices:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=prices_text, parse_mode="HTML")

    # Отправляем блок новостей
    if important_updates:
        result_text = "🔔 <b>Важные обновления по твоим активностям:</b>\n\n" + "\n\n".join(important_updates)
        if silent_projects_count > 0:
            result_text += f"\n\n🤫 По остальным проектам ({silent_projects_count} шт.) - важных новостей не обнаружено."
    else:
        result_text = f"👌 По всем проектам из таблицы в плане новостей сейчас полное затишье. Критических дедлайнов нет."

    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=result_text, parse_mode="HTML")

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("У вас нет доступа к этому боту.")
        return

    # Инлайн-меню с кнопками
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Портфель", callback_data="portfolio")],
        [InlineKeyboardButton(text="📰 Дайджест дедлайнов", callback_data="digest")],
        [InlineKeyboardButton(text="🌡 Пульс рынка", callback_data="pulse")]
    ])

    await message.answer(
        f"Привет! Твой крипто-терминал обновлен.\n\n"
        f"Доступные команды:\n"
        f"/check - запустить аудит цен и новостей\n"
        f"/pulse - проверить рыночный пульс\n\n"
        f"Или используй кнопки ниже:",
        reply_markup=keyboard
    )

@dp.message(Command("check"))
async def manual_check(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    await send_daily_digest()

@dp.message(Command("pulse"))
async def pulse_cmd(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    pulse_text = await get_market_pulse_text()
    await message.answer(pulse_text, parse_mode="HTML")

# --- ОБРАБОТЧИКИ CALLBACK ЗАПРОСОВ ---
@dp.callback_query(lambda c: c.data and c.data.startswith("why_"))
async def handle_volatility_research(callback: CallbackQuery):
    """Обработчик кнопки 'Узнать причину' для алертов волатильности"""
    await callback.answer("Ищу причину скачка цен...")

    # Извлекаем название монеты из callback_data
    coin_name = callback.data.replace("why_", "")

    # Показываем индикатор загрузки
    await callback.message.answer("🔎 Анализирую причины движения цены...")

    # Выполняем поиск причины
    loop = asyncio.get_event_loop()
    reason = await loop.run_in_executor(None, search_volatility_reason, coin_name)

    # Отправляем результат
    result_text = f"🔍 <b>Причина волатильности {coin_name.upper()}:</b>\n\n{reason}"
    await callback.message.answer(result_text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "portfolio")
async def handle_portfolio_button(callback: CallbackQuery):
    """Обработчик кнопки 'Портфель'"""
    await callback.answer()
    await send_daily_digest()

@dp.callback_query(lambda c: c.data == "digest")
async def handle_digest_button(callback: CallbackQuery):
    """Обработчик кнопки 'Дайджест дедлайнов'"""
    await callback.answer()
    await send_daily_digest()

@dp.callback_query(lambda c: c.data == "pulse")
async def handle_pulse_button(callback: CallbackQuery):
    """Обработчик кнопки 'Пульс рынка'"""
    await callback.answer()
    pulse_text = await get_market_pulse_text()
    await callback.message.answer(pulse_text, parse_mode="HTML")

async def main():
    # Запуск утреннего дайджеста в 09:00
    scheduler.add_job(send_daily_digest, "cron", hour=9, minute=0)

    # Запуск проверки волатильности каждые 15 минут
    scheduler.add_job(check_volatility_alerts, "interval", minutes=15)

    scheduler.start()
    print("✅ Бот запущен. Утренний дайджест: 09:00. Проверка волатильности: каждые 15 минут.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
