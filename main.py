import os
import json
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import OpenAI
from tavily import TavilyClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Загрузка переменных окружения из .env файла
load_dotenv()

# --- FSM СОСТОЯНИЯ ДЛЯ ДОБАВЛЕНИЯ АКТИВОВ ---
class AddSpotState(StatesGroup):
    waiting_for_spot_data = State()

class AddAirdropState(StatesGroup):
    waiting_for_airdrop_data = State()

# --- FSM СОСТОЯНИЯ ДЛЯ УПРАВЛЕНИЯ ПОЗИЦИЯМИ ---
class ManagePositionState(StatesGroup):
    waiting_for_buy_amount = State()
    waiting_for_buy_price = State()
    waiting_for_sell_amount = State()
    waiting_for_sell_price = State()
    waiting_for_new_target = State()

# --- НАСТРОЙКА КЛЮЧЕЙ И ТАБЛИЦЫ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
MY_TELEGRAM_ID = os.getenv("MY_TELEGRAM_ID")

if not all([TELEGRAM_TOKEN, OPENROUTER_API_KEY, TAVILY_API_KEY, GOOGLE_SHEETS_ID, MY_TELEGRAM_ID]):
    raise ValueError("Отсутствуют обязательные переменные окружения: TELEGRAM_TOKEN, OPENROUTER_API_KEY, TAVILY_API_KEY, GOOGLE_SHEETS_ID, MY_TELEGRAM_ID")

ADMIN_CHAT_ID = int(MY_TELEGRAM_ID)
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# --- НАСТРОЙКА GOOGLE SHEETS API ---
def init_gspread():
    """Инициализация gspread клиента"""
    try:
        # Проверяем наличие credentials.json
        if os.path.exists("credentials.json"):
            scope = ['https://spreadsheets.google.com/feeds',
                     'https://www.googleapis.com/auth/drive']
            creds = Credentials.from_service_account_file('credentials.json', scopes=scope)
            client = gspread.authorize(creds)
            return client
        else:
            print("WARNING: credentials.json not found. Add asset function will be unavailable.")
            return None
    except Exception as e:
        print(f"WARNING: Error initializing gspread: {e}")
        return None

gspread_client = init_gspread()

# Инициализация OpenRouter клиента
openai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# --- ФУНКЦИИ РАБОТЫ С GOOGLE SHEETS ---
def add_spot_to_sheet(ticker, quantity, entry_price, take_profit):
    """Добавляет новую позицию в лист Spot"""
    try:
        if not gspread_client:
            return False, "Google Sheets API не настроен (отсутствует credentials.json)"

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)  # Первый лист (Spot)

        # Добавляем новую строку
        worksheet.append_row([ticker.upper(), quantity, entry_price, take_profit, ""])
        return True, f"✅ Добавлено: {ticker.upper()} (Вход: ${entry_price}, Тейк: ${take_profit})"
    except Exception as e:
        return False, f"❌ Ошибка добавления в Google Sheets: {str(e)}"

def add_airdrop_to_sheet(project, activity_type, status, deadline):
    """Добавляет новую активность в лист Airdrops"""
    try:
        if not gspread_client:
            return False, "Google Sheets API не настроен (отсутствует credentials.json)"

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        # Получаем лист по gid (878500138)
        worksheet = None
        for ws in spreadsheet.worksheets():
            if ws.id == 878500138:
                worksheet = ws
                break

        if not worksheet:
            # Если не нашли по ID, пробуем второй лист
            worksheet = spreadsheet.get_worksheet(1)

        # Добавляем новую строку
        worksheet.append_row([project, activity_type, deadline, status])
        return True, f"✅ Добавлено: {project} [{activity_type}] - {status}"
    except Exception as e:
        return False, f"❌ Ошибка добавления в Google Sheets: {str(e)}"

def get_spot_positions_with_rows():
    """Получает список всех позиций из Spot листа с номерами строк"""
    try:
        if not gspread_client:
            return []

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)  # Spot лист
        all_values = worksheet.get_all_values()

        positions = []
        for idx, row in enumerate(all_values[1:], start=2):  # Пропускаем заголовок
            if len(row) > 0 and row[0].strip():
                ticker = row[0].strip().upper()
                quantity = float(row[1]) if len(row) > 1 and row[1] else 0
                entry_price = float(row[2]) if len(row) > 2 and row[2] else 0
                take_profit = float(row[3]) if len(row) > 3 and row[3] else 0

                positions.append({
                    "row": idx,
                    "ticker": ticker,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "take_profit": take_profit
                })

        return positions
    except Exception as e:
        print(f"Error getting spot positions: {e}")
        return []

def update_position_buy(ticker, row_num, new_quantity, new_avg_price):
    """Обновляет позицию после докупки"""
    try:
        if not gspread_client:
            return False, "Google Sheets API не настроен"

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)

        worksheet.update_cell(row_num, 2, new_quantity)  # Колонка B - количество
        worksheet.update_cell(row_num, 3, new_avg_price)  # Колонка C - средняя цена

        return True, f"✅ Обновлено: {ticker} - новое кол-во {new_quantity}, средняя цена ${new_avg_price:.6f}"
    except Exception as e:
        return False, f"❌ Ошибка обновления: {str(e)}"

def update_position_sell(ticker, row_num, new_quantity):
    """Обновляет позицию после продажи"""
    try:
        if not gspread_client:
            return False, "Google Sheets API не настроен"

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)

        if new_quantity <= 0:
            # Удаляем строку, если позиция полностью закрыта
            worksheet.delete_rows(row_num)
            return True, f"✅ Позиция {ticker} полностью закрыта и удалена"
        else:
            worksheet.update_cell(row_num, 2, new_quantity)
            return True, f"✅ Обновлено: {ticker} - осталось {new_quantity}"
    except Exception as e:
        return False, f"❌ Ошибка обновления: {str(e)}"

def update_take_profit(ticker, row_num, new_target):
    """Обновляет цель (тейк-профит)"""
    try:
        if not gspread_client:
            return False, "Google Sheets API не настроен"

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)

        worksheet.update_cell(row_num, 4, new_target)  # Колонка D - тейк

        return True, f"✅ Цель обновлена: {ticker} -> ${new_target}"
    except Exception as e:
        return False, f"❌ Ошибка обновления: {str(e)}"

def ensure_profit_history_sheet():
    """Проверяет наличие листа История сделок, создает если нет"""
    try:
        if not gspread_client:
            return None

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)

        # Ищем лист "История сделок"
        for ws in spreadsheet.worksheets():
            if ws.title.lower() in ["история сделок", "profit history", "история"]:
                return ws

        # Создаем новый лист
        worksheet = spreadsheet.add_worksheet(title="История сделок", rows=100, cols=7)
        # Добавляем заголовки
        worksheet.append_row(["Дата", "Тикер", "Количество", "Вход $", "Выход $", "Профит $", "Профит %"])
        return worksheet

    except Exception as e:
        print(f"Error ensuring profit history sheet: {e}")
        return None

def add_profit_record(ticker, quantity, entry_price, exit_price, profit_usd, profit_pct):
    """Добавляет запись о зафиксированном профите"""
    try:
        worksheet = ensure_profit_history_sheet()
        if not worksheet:
            return False, "Не удалось создать/найти лист История сделок"

        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        worksheet.append_row([
            date_str,
            ticker,
            quantity,
            entry_price,
            exit_price,
            profit_usd,
            profit_pct
        ])

        return True, f"✅ Записано в историю: {ticker} | Профит: ${profit_usd:.2f} ({profit_pct:+.1f}%)"
    except Exception as e:
        return False, f"❌ Ошибка записи в историю: {str(e)}"

# Кэш алертов волатильности: {coin_name: last_alert_timestamp}
volatility_alerts_cache = {}

# Маппинг популярных тикеров в CoinGecko coin_id
COIN_MAP = {
    "ARB": "arbitrum", "ARBITRUM": "arbitrum",
    "OP": "optimism", "OPTIMISM": "optimism",
    "ATOM": "cosmos",
    "TIA": "celestia",
    "DYM": "dymension",
    "APT": "aptos",
    "BONK": "bonk",
    "APEX": "apex-token-2",
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "AAVE": "aave",
    "STRK": "starknet",
    "ZK": "zksync",
    "SUI": "sui",
    "SEI": "sei-network",
    "INJ": "injective-protocol",
    "OSMO": "osmosis",
    "KUJI": "kujira",
    "JUNO": "juno-network",
    "EVMOS": "evmos",
    "AXL": "axelar",
    "CRO": "crypto-com-chain",
    "FTM": "fantom",
    "NEAR": "near",
    "ADA": "cardano",
    "XRP": "ripple",
    "BNB": "binancecoin",
    "DOGE": "dogecoin"
}

# Функция получения всех цен одним запросом
def fetch_all_prices():
    """Получает цены для всех монет из COIN_MAP одним запросом"""
    ids_str = ",".join(set(COIN_MAP.values()))
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ids_str,
        "vs_currencies": "usd",
        "include_24hr_change": "true"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"CoinGecko Error: {e}")
        return {}

# Функция чтения спот-портфеля из Google Таблицы (лист Spot)
def get_spot_portfolio():
    """Читает спот-портфель: [Тикер, Количество, Вход, Тейк, Заметка]"""
    try:
        # gid=0 обычно первый лист (Spot)
        url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv&gid=0"
        df = pd.read_csv(url)

        spot_list = []
        for _, row in df.iterrows():
            ticker = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not ticker:
                continue

            # Колонки: Тикер, Количество, Вход, Тейк, Заметка
            quantity = float(row.iloc[1]) if len(row) > 1 and pd.notna(row.iloc[1]) else 0
            entry_price = None
            take_profit = None
            note = ""

            if len(row) > 2 and pd.notna(row.iloc[2]):
                try:
                    entry_price = float(str(row.iloc[2]).replace("$", "").replace(",", ".").strip())
                except ValueError:
                    pass

            if len(row) > 3 and pd.notna(row.iloc[3]):
                try:
                    take_profit = float(str(row.iloc[3]).replace("$", "").replace(",", ".").strip())
                except ValueError:
                    pass

            if len(row) > 4 and pd.notna(row.iloc[4]):
                note = str(row.iloc[4]).strip()

            spot_list.append({
                "ticker": ticker.upper(),
                "quantity": quantity,
                "entry_price": entry_price,
                "take_profit": take_profit,
                "note": note
            })

        return spot_list
    except Exception as e:
        print(f"Ошибка чтения спот-портфеля: {e}")
        return []

# Функция чтения аирдропов из Google Таблицы (лист Airdrops)
def get_airdrops_activities():
    """Читает активности/аирдропы: [Проект, Тип, Дедлайн, Статус]"""
    try:
        # gid=878500138 - лист Airdrops/Активности
        url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv&gid=878500138"
        df = pd.read_csv(url)

        activities_list = []
        for _, row in df.iterrows():
            project = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not project:
                continue

            # Колонки: Проект, Тип, Дедлайн, Статус
            activity_type = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else "Аирдроп"
            deadline = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            status = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else "Активен"

            activities_list.append({
                "project": project,
                "type": activity_type,
                "deadline": deadline,
                "status": status
            })

        return activities_list
    except Exception as e:
        print(f"Ошибка чтения аирдропов: {e}")
        return []

# Функция чтения проектов и цен покупки из Google Таблицы (DEPRECATED - оставлена для совместимости)
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

# Функция пакетного получения цен с CoinGecko API
def get_batch_crypto_prices(coin_ids):
    """Получает цены для нескольких монет одним запросом"""
    try:
        if not coin_ids:
            return {}

        # Формируем список coin_id через запятую
        ids_string = ",".join(coin_ids)
        url = f"https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": ids_string,
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        response = requests.get(url, params=params, timeout=10).json()
        return response
    except Exception as e:
        print(f"Ошибка пакетного запроса цен: {e}")
        return {}

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

        ВАЖНО HTML-РАЗМЕТКА:
        - Используй ТОЛЬКО теги: <b>, </b>, <i>, </i>, <a href="URL">, </a>
        - НИКОГДА не используй символы < и > в обычном тексте
        - НЕ используй теги: <br>, </br>, <p>, </p>, <code>, <pre>, <u>
        - Для переноса строки используй обычный Enter (перенос строки), НЕ используй <br>

        Пиши строго без воды. Используй только обычные дефисы вместо длинных тире.

        Данные из сети:
        {news_text}
        """

        response = openai_client.chat.completions.create(
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        # Очистка от запрещенных HTML тегов
        result = result.replace("<br>", "\n").replace("</br>", "").replace("<br/>", "\n")
        result = result.replace("<p>", "").replace("</p>", "\n")
        return result
    except Exception as e:
        error_msg = f"Ошибка OpenRouter (анализ {project_name}): {str(e)}"
        print(error_msg)
        return error_msg

# Функция анализа новостей проекта (обертка для async)
def analyze_project_news(project_name):
    try:
        search_result = search_news_tavily(project_name)
        if not search_result:
            error_msg = f"❌ Ошибка Tavily: не удалось найти новости для {project_name}"
            print(error_msg)
            return error_msg
        return analyze_with_openai(project_name, search_result)
    except Exception as e:
        error_msg = f"❌ Ошибка поиска новостей ({project_name}): {str(e)}"
        print(error_msg)
        return error_msg

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

        # Анализируем через OpenRouter
        prompt = f"""
        Ты - крипто-аналитик. Объясни в 2-3 предложениях, почему резко изменилась цена токена {project_name}.

        Ищи конкретные триггеры: листинг на бирже, важные новости, взлом, регуляторные решения, действия китов, технические проблемы.

        Если причина не ясна из новостей, так и скажи честно.

        ВАЖНО HTML-РАЗМЕТКА:
        - Используй ТОЛЬКО теги: <b>, </b>, <i>, </i>, <a href="URL">, </a>
        - НИКОГДА не используй символы < и > в обычном тексте
        - НЕ используй теги: <br>, </br>, <p>, </p>, <code>, <pre>, <u>
        - Для переноса строки используй обычный Enter (перенос строки), НЕ используй <br>
        - Включай ссылки на источники: <a href="URL">текст</a>

        Данные:
        {news_text}
        """

        response = openai_client.chat.completions.create(
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )

        result = response.choices[0].message.content.strip()
        # Очистка от запрещенных HTML тегов
        result = result.replace("<br>", "\n").replace("</br>", "").replace("<br/>", "\n")
        result = result.replace("<p>", "").replace("</p>", "\n")
        return result

    except Exception as e:
        error_msg = f"❌ Ошибка поиска причины волатильности: {str(e)}"
        print(error_msg)
        return error_msg

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

# Функция быстрого просмотра портфеля (без анализа новостей)
async def send_portfolio_view():
    """Быстрый просмотр портфеля: спот-позиции и активности"""
    loop = asyncio.get_event_loop()

    # Получаем данные из двух листов
    spot_data = await loop.run_in_executor(None, get_spot_portfolio)
    airdrops_data = await loop.run_in_executor(None, get_airdrops_activities)

    if not spot_data and not airdrops_data:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text="❌ Не удалось загрузить данные из Google Таблицы.")
        return

    # --- СЕКЦИЯ 1: СПОТ-ПОРТФЕЛЬ ---
    if spot_data:
        # Получаем все цены одним запросом
        prices_data = await loop.run_in_executor(None, fetch_all_prices)

        spot_text = "💼 <b>СПОТ-ПОРТФЕЛЬ & ЦЕЛЕВЫЕ ЦЕНЫ:</b>\n\n"

        for spot in spot_data:
            ticker = spot["ticker"].upper()
            entry_price = spot["entry_price"] if spot["entry_price"] else 0
            tp_price = spot["take_profit"] if spot["take_profit"] else 0

            # Получаем coin_id из маппинга
            coin_id = COIN_MAP.get(ticker)

            # Проверяем, есть ли данные по этой монете
            if coin_id and coin_id in prices_data:
                coin_data = prices_data[coin_id]
                current_price = coin_data.get("usd")

                if current_price and current_price > 0:
                    # Расчет PnL с защитой от деления на 0
                    pnl = 0
                    if entry_price > 0:
                        pnl = ((current_price - entry_price) / entry_price) * 100
                    emoji = "🟢" if pnl >= 0 else "🔴"

                    # Расчет расстояния до тейка с защитой от деления на 0
                    tp_gain = None
                    if tp_price > 0 and current_price > 0:
                        tp_gain = ((tp_price - current_price) / current_price) * 100

                    # Форматирование цен
                    price_fmt = f"{current_price:.6f}" if current_price < 0.01 else f"{current_price:.4f}"
                    entry_fmt = f"{entry_price:.6f}" if entry_price < 0.01 and entry_price > 0 else f"{entry_price:.4f}" if entry_price > 0 else "N/A"
                    tp_fmt = f"{tp_price:.6f}" if tp_price < 0.01 and tp_price > 0 else f"{tp_price:.2f}" if tp_price > 0 else "N/A"

                    # Строка расстояния до тейка - строго N/A если невозможно рассчитать
                    if tp_gain is not None and entry_price > 0 and current_price > 0:
                        tp_str = f"+{tp_gain:.1f}%" if tp_gain >= 0 else f"{tp_gain:.1f}%"
                    else:
                        tp_str = "N/A"

                    # Формируем строку
                    pnl_str = f"{pnl:+.1f}%" if entry_price > 0 else "N/A"
                    line = (
                        f"{emoji} <b>{ticker}</b>: ${price_fmt} (Вход: ${entry_fmt} | Тейк: ${tp_fmt} | PnL: {pnl_str})\n"
                        f"   🎯 До тейка: {tp_str}\n"
                    )
                    spot_text += line
                else:
                    spot_text += f"• <b>{ticker}</b>: Цена недоступна\n"
            else:
                spot_text += f"• <b>{ticker}</b>: Не найден в CoinGecko\n"

        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=spot_text, parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка отправки спот-портфеля: {e}")
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=spot_text)

    # --- СЕКЦИЯ 2: РАДАР АКТИВНОСТЕЙ & СТЕЙКИНГА ---
    if airdrops_data:
        activities_text = "⏳ <b>РАДАР АКТИВНОСТЕЙ & СТЕЙКИНГА:</b>\n\n"

        for activity in airdrops_data:
            project = activity["project"]
            activity_type = activity["type"]
            deadline = activity["deadline"]
            status = activity["status"]

            activities_text += f"• <b>{project}</b> [Тип: {activity_type}]"

            if status:
                activities_text += f" — Статус: {status}"

            if deadline:
                activities_text += f" (Дедлайн: {deadline})"

            activities_text += "\n"

        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=activities_text, parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка отправки активностей: {e}")
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=activities_text)

# Главная функция утреннего дайджеста
async def send_daily_digest():
    try:
        # Получаем рыночный пульс для шапки
        pulse_text = await get_market_pulse_text()

        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"{pulse_text}\n\n🔍 Начинаю аудит портфолио и поиск крипто-новостей...",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Ошибка отправки начального сообщения с HTML: {e}")
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"{pulse_text}\n\nНачинаю аудит портфолио и поиск крипто-новостей..."
            )
    except Exception as e:
        error_msg = f"❌ Ошибка получения рыночного пульса: {str(e)}"
        print(error_msg)
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=error_msg)
        # Продолжаем без пульса
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🔍 Начинаю аудит портфолио и поиск крипто-новостей..."
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
        if curr_price and change24h is not None:
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
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=prices_text, parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка отправки цен с HTML: {e}")
            # Отправляем без HTML разметки как фоллбэк
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=prices_text)
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ Ошибка HTML разметки: {str(e)}")

    # Отправляем блок новостей
    if important_updates:
        result_text = "🔔 <b>Важные обновления по твоим активностям:</b>\n\n" + "\n\n".join(important_updates)
        if silent_projects_count > 0:
            result_text += f"\n\n🤫 По остальным проектам ({silent_projects_count} шт.) - важных новостей не обнаружено."
    else:
        result_text = f"👌 По всем проектам из таблицы в плане новостей сейчас полное затишье. Критических дедлайнов нет."

    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=result_text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка отправки новостей с HTML: {e}")
        # Отправляем без HTML разметки как фоллбэк
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=result_text)
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ Ошибка HTML разметки в новостях: {str(e)}")

# Функция дайджеста новостей (без цен, только новости и дедлайны)
async def send_news_digest():
    """Анализ новостей и поиск критических триггеров только по проектам из Airdrops"""
    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text="📰 Начинаю поиск важных новостей и дедлайнов по вашим активностям..."
    )

    loop = asyncio.get_event_loop()
    # Читаем только лист Airdrops для анализа новостей
    airdrops_data = await loop.run_in_executor(None, get_airdrops_activities)

    if not airdrops_data:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text="❌ Нет активностей для мониторинга в листе Airdrops.")
        return

    important_updates = []
    silent_projects_count = 0

    for activity in airdrops_data:
        project = activity["project"]

        # Ищем важные новости через ИИ
        report = await loop.run_in_executor(None, analyze_project_news, project)
        if report.lower() == "тишина":
            silent_projects_count += 1
        else:
            important_updates.append(f"🔥 <b>{project}</b>:\n{report}")

        await asyncio.sleep(2)

    # Отправляем блок новостей
    if important_updates:
        result_text = "🔔 <b>Важные обновления по твоим активностям:</b>\n\n" + "\n\n".join(important_updates)
        if silent_projects_count > 0:
            result_text += f"\n\n🤫 По остальным проектам ({silent_projects_count} шт.) - важных новостей не обнаружено."
    else:
        result_text = f"👌 По всем проектам из активностей в плане новостей сейчас полное затишье. Критических дедлайнов нет."

    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=result_text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка отправки дайджеста с HTML: {e}")
        # Отправляем без HTML разметки как фоллбэк
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=result_text)
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ Ошибка HTML разметки: {str(e)}")

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("У вас нет доступа к этому боту.")
        return

    # Инлайн-меню с кнопками
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Портфель", callback_data="portfolio")],
        [InlineKeyboardButton(text="✏️ Управление активами", callback_data="manage_assets")],
        [InlineKeyboardButton(text="📰 Дайджест дедлайнов", callback_data="digest")],
        [InlineKeyboardButton(text="🌡 Пульс рынка", callback_data="pulse")],
        [InlineKeyboardButton(text="➕ Добавить актив", callback_data="add_asset")]
    ])

    await message.answer(
        f"Привет! Твой крипто-терминал обновлен.\n\n"
        f"Доступные команды:\n"
        f"/portfolio - быстрый просмотр портфеля\n"
        f"/manage - управление активами\n"
        f"/digest - дайджест новостей и дедлайнов\n"
        f"/check - полный аудит (цены + новости)\n"
        f"/pulse - проверить рыночный пульс\n"
        f"/add - добавить актив в таблицу\n\n"
        f"Или используй кнопки ниже:",
        reply_markup=keyboard
    )

@dp.message(Command("portfolio"))
async def portfolio_cmd(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    await send_portfolio_view()

@dp.message(Command("digest"))
async def digest_cmd(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    await send_news_digest()

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

@dp.message(Command("add"))
async def add_cmd(message: Message):
    """Команда для добавления актива"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    if not gspread_client:
        await message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 В Спот-портфель", callback_data="add_spot")],
        [InlineKeyboardButton(text="🪂 В Радар активностей", callback_data="add_airdrop")]
    ])

    await message.answer("➕ Выберите, куда добавить актив:", reply_markup=keyboard)

@dp.message(Command("manage"))
async def manage_cmd(message: Message):
    """Команда для управления активами"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    if not gspread_client:
        await message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    loop = asyncio.get_event_loop()
    positions = await loop.run_in_executor(None, get_spot_positions_with_rows)

    if not positions:
        await message.answer("📊 Спот-портфель пуст. Добавьте активы через /add")
        return

    # Формируем инлайн-кнопки для каждой монеты
    keyboard_buttons = []
    for pos in positions:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"{pos['ticker']} ({pos['quantity']} шт.)",
                callback_data=f"manage_{pos['ticker']}_{pos['row']}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("✏️ <b>Выберите актив для управления:</b>", reply_markup=keyboard, parse_mode="HTML")

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
    """Обработчик кнопки 'Портфель' - только цены, без новостей"""
    await callback.answer()
    await send_portfolio_view()

@dp.callback_query(lambda c: c.data == "digest")
async def handle_digest_button(callback: CallbackQuery):
    """Обработчик кнопки 'Дайджест дедлайнов' - только новости"""
    await callback.answer()
    await send_news_digest()

@dp.callback_query(lambda c: c.data == "pulse")
async def handle_pulse_button(callback: CallbackQuery):
    """Обработчик кнопки 'Пульс рынка'"""
    await callback.answer()
    pulse_text = await get_market_pulse_text()
    await callback.message.answer(pulse_text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "add_asset")
async def handle_add_asset_button(callback: CallbackQuery):
    """Обработчик кнопки 'Добавить актив'"""
    await callback.answer()

    if not gspread_client:
        await callback.message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 В Спот-портфель", callback_data="add_spot")],
        [InlineKeyboardButton(text="🪂 В Радар активностей", callback_data="add_airdrop")]
    ])

    await callback.message.answer("➕ Выберите, куда добавить актив:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "add_spot")
async def handle_add_spot(callback: CallbackQuery, state: FSMContext):
    """Начало добавления актива в Спот"""
    await callback.answer()
    await callback.message.answer(
        "💼 <b>Добавление в Спот-портфель</b>\n\n"
        "Введите данные в формате:\n"
        "<code>ТИКЕР КОЛИЧЕСТВО ВХОД ТЕЙК</code>\n\n"
        "Пример: <code>NEAR 100 4.5 12.0</code>\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="HTML"
    )
    await state.set_state(AddSpotState.waiting_for_spot_data)

@dp.callback_query(lambda c: c.data == "add_airdrop")
async def handle_add_airdrop(callback: CallbackQuery, state: FSMContext):
    """Начало добавления активности в Радар"""
    await callback.answer()
    await callback.message.answer(
        "🪂 <b>Добавление в Радар активностей</b>\n\n"
        "Введите данные в формате:\n"
        "<code>ПРОЕКТ | ТИП | СТАТУС | ДЕДЛАЙН</code>\n\n"
        "Пример: <code>Berachain | Тестнет | Квесты выполнены | Q4 2026</code>\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="HTML"
    )
    await state.set_state(AddAirdropState.waiting_for_airdrop_data)

@dp.callback_query(lambda c: c.data == "manage_assets")
async def handle_manage_assets_button(callback: CallbackQuery):
    """Обработчик кнопки 'Управление активами'"""
    await callback.answer()

    if not gspread_client:
        await callback.message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    loop = asyncio.get_event_loop()
    positions = await loop.run_in_executor(None, get_spot_positions_with_rows)

    if not positions:
        await callback.message.answer("📊 Спот-портфель пуст. Добавьте активы через /add")
        return

    # Формируем инлайн-кнопки для каждой монеты
    keyboard_buttons = []
    for pos in positions:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"{pos['ticker']} ({pos['quantity']} шт.)",
                callback_data=f"manage_{pos['ticker']}_{pos['row']}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("✏️ <b>Выберите актив для управления:</b>", reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(lambda c: c.data and c.data.startswith("manage_"))
async def handle_manage_position(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора позиции для управления"""
    await callback.answer()

    # Парсим callback_data: manage_TICKER_ROW
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.message.answer("❌ Ошибка данных")
        return

    ticker = parts[1]
    row_num = int(parts[2])

    # Получаем позиции и находим нужную
    loop = asyncio.get_event_loop()
    positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
    position = next((p for p in positions if p['ticker'] == ticker and p['row'] == row_num), None)

    if not position:
        await callback.message.answer("❌ Позиция не найдена")
        return

    # Получаем текущую цену
    prices_data = await loop.run_in_executor(None, fetch_all_prices)
    coin_id = COIN_MAP.get(ticker)
    current_price = 0

    if coin_id and coin_id in prices_data:
        current_price = prices_data[coin_id].get("usd", 0)

    # Формируем карточку актива
    entry_price = position['entry_price']
    quantity = position['quantity']
    take_profit = position['take_profit']

    pnl = 0
    if entry_price > 0 and current_price > 0:
        pnl = ((current_price - entry_price) / entry_price) * 100

    price_fmt = f"{current_price:.6f}" if current_price < 0.01 else f"{current_price:.4f}"
    entry_fmt = f"{entry_price:.6f}" if entry_price < 0.01 else f"{entry_price:.4f}"
    tp_fmt = f"{take_profit:.6f}" if take_profit < 0.01 else f"{take_profit:.2f}"

    card_text = (
        f"💼 <b>{ticker}</b>\n\n"
        f"📊 Количество: <b>{quantity}</b>\n"
        f"💵 Цена входа: <b>${entry_fmt}</b>\n"
        f"💰 Текущая цена: <b>${price_fmt}</b>\n"
        f"🎯 Цель (Тейк): <b>${tp_fmt}</b>\n"
        f"📈 PnL: <b>{pnl:+.1f}%</b>"
    )

    # Сохраняем данные в state для последующих операций
    await state.update_data(ticker=ticker, row_num=row_num, position=position)

    # Кнопки управления
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Докупить", callback_data=f"buy_{ticker}_{row_num}")],
        [InlineKeyboardButton(text="💰 Зафиксировать прибыль", callback_data=f"sell_{ticker}_{row_num}")],
        [InlineKeyboardButton(text="🎯 Изменить цель (Тейк)", callback_data=f"target_{ticker}_{row_num}")]
    ])

    await callback.message.answer(card_text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def handle_buy_position(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Докупить'"""
    await callback.answer()

    parts = callback.data.split("_")
    ticker = parts[1]
    row_num = int(parts[2])

    await state.update_data(ticker=ticker, row_num=row_num, action="buy")

    await callback.message.answer(
        f"➕ <b>Докупка {ticker}</b>\n\n"
        f"Введите данные в формате:\n"
        f"<code>КОЛИЧЕСТВО ЦЕНА</code>\n\n"
        f"Пример: <code>50 5.2</code>\n\n"
        f"Отправьте /cancel для отмены.",
        parse_mode="HTML"
    )
    await state.set_state(ManagePositionState.waiting_for_buy_amount)

@dp.callback_query(lambda c: c.data and c.data.startswith("sell_"))
async def handle_sell_position(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Зафиксировать прибыль'"""
    await callback.answer()

    parts = callback.data.split("_")
    ticker = parts[1]
    row_num = int(parts[2])

    await state.update_data(ticker=ticker, row_num=row_num, action="sell")

    await callback.message.answer(
        f"💰 <b>Фиксация профита {ticker}</b>\n\n"
        f"Введите данные в формате:\n"
        f"<code>КОЛИЧЕСТВО ЦЕНА_ПРОДАЖИ</code>\n\n"
        f"Пример: <code>30 8.5</code>\n\n"
        f"Отправьте /cancel для отмены.",
        parse_mode="HTML"
    )
    await state.set_state(ManagePositionState.waiting_for_sell_amount)

@dp.callback_query(lambda c: c.data and c.data.startswith("target_"))
async def handle_change_target(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Изменить цель'"""
    await callback.answer()

    parts = callback.data.split("_")
    ticker = parts[1]
    row_num = int(parts[2])

    await state.update_data(ticker=ticker, row_num=row_num, action="target")

    await callback.message.answer(
        f"🎯 <b>Изменение цели {ticker}</b>\n\n"
        f"Введите новую целевую цену:\n\n"
        f"Пример: <code>15.0</code>\n\n"
        f"Отправьте /cancel для отмены.",
        parse_mode="HTML"
    )
    await state.set_state(ManagePositionState.waiting_for_new_target)

# --- ОБРАБОТЧИКИ FSM СОСТОЯНИЙ ---
@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных действий для отмены.")
        return

    await state.clear()
    await message.answer("❌ Действие отменено.")

@dp.message(AddSpotState.waiting_for_spot_data)
async def process_spot_data(message: Message, state: FSMContext):
    """Обработка данных для добавления в Спот"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        # Парсим формат: ТИКЕР КОЛИЧЕСТВО ВХОД ТЕЙК
        parts = message.text.strip().split()
        if len(parts) != 4:
            await message.answer("❌ Неверный формат. Используйте: ТИКЕР КОЛИЧЕСТВО ВХОД ТЕЙК\nПример: NEAR 100 4.5 12.0")
            return

        ticker = parts[0].upper()
        quantity = float(parts[1])
        entry_price = float(parts[2])
        take_profit = float(parts[3])

        # Добавляем в таблицу
        success, result_msg = add_spot_to_sheet(ticker, quantity, entry_price, take_profit)
        await message.answer(result_msg, parse_mode="HTML")

        if success:
            await state.clear()

    except ValueError:
        await message.answer("❌ Ошибка: количество, вход и тейк должны быть числами.\nПример: NEAR 100 4.5 12.0")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(AddAirdropState.waiting_for_airdrop_data)
async def process_airdrop_data(message: Message, state: FSMContext):
    """Обработка данных для добавления в Радар активностей"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        # Парсим формат: ПРОЕКТ | ТИП | СТАТУС | ДЕДЛАЙН
        parts = [p.strip() for p in message.text.split("|")]
        if len(parts) != 4:
            await message.answer(
                "❌ Неверный формат. Используйте:\n"
                "ПРОЕКТ | ТИП | СТАТУС | ДЕДЛАЙН\n\n"
                "Пример: Berachain | Тестнет | Квесты выполнены | Q4 2026"
            )
            return

        project = parts[0]
        activity_type = parts[1]
        status = parts[2]
        deadline = parts[3]

        # Добавляем в таблицу
        success, result_msg = add_airdrop_to_sheet(project, activity_type, status, deadline)
        await message.answer(result_msg, parse_mode="HTML")

        if success:
            await state.clear()

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(ManagePositionState.waiting_for_buy_amount)
async def process_buy_data(message: Message, state: FSMContext):
    """Обработка докупки"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        data = await state.get_data()
        ticker = data.get("ticker")
        row_num = data.get("row_num")

        # Парсим: КОЛИЧЕСТВО ЦЕНА
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: КОЛИЧЕСТВО ЦЕНА\nПример: 50 5.2")
            return

        buy_quantity = float(parts[0])
        buy_price = float(parts[1])

        # Получаем текущую позицию
        loop = asyncio.get_event_loop()
        positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
        position = next((p for p in positions if p['ticker'] == ticker and p['row'] == row_num), None)

        if not position:
            await message.answer("❌ Позиция не найдена")
            await state.clear()
            return

        # Рассчитываем новую среднюю цену
        old_quantity = position['quantity']
        old_price = position['entry_price']

        new_quantity = old_quantity + buy_quantity
        new_avg_price = ((old_quantity * old_price) + (buy_quantity * buy_price)) / new_quantity

        # Обновляем в таблице
        success, result_msg = await loop.run_in_executor(
            None, update_position_buy, ticker, row_num, new_quantity, new_avg_price
        )

        await message.answer(result_msg, parse_mode="HTML")

        if success:
            await state.clear()

    except ValueError:
        await message.answer("❌ Ошибка: количество и цена должны быть числами")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(ManagePositionState.waiting_for_sell_amount)
async def process_sell_data(message: Message, state: FSMContext):
    """Обработка фиксации профита"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        data = await state.get_data()
        ticker = data.get("ticker")
        row_num = data.get("row_num")

        # Парсим: КОЛИЧЕСТВО ЦЕНА_ПРОДАЖИ
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: КОЛИЧЕСТВО ЦЕНА_ПРОДАЖИ\nПример: 30 8.5")
            return

        sell_quantity = float(parts[0])
        sell_price = float(parts[1])

        # Получаем текущую позицию
        loop = asyncio.get_event_loop()
        positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
        position = next((p for p in positions if p['ticker'] == ticker and p['row'] == row_num), None)

        if not position:
            await message.answer("❌ Позиция не найдена")
            await state.clear()
            return

        old_quantity = position['quantity']
        entry_price = position['entry_price']

        if sell_quantity > old_quantity:
            await message.answer(f"❌ Недостаточно монет. Доступно: {old_quantity}")
            return

        # Рассчитываем профит
        profit_usd = (sell_price - entry_price) * sell_quantity
        profit_pct = ((sell_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

        # Обновляем количество в таблице
        new_quantity = old_quantity - sell_quantity
        success, result_msg = await loop.run_in_executor(
            None, update_position_sell, ticker, row_num, new_quantity
        )

        if not success:
            await message.answer(result_msg)
            return

        # Добавляем запись в историю
        success_history, history_msg = await loop.run_in_executor(
            None, add_profit_record, ticker, sell_quantity, entry_price, sell_price, profit_usd, profit_pct
        )

        result_text = (
            f"✅ <b>Профит зафиксирован!</b>\n\n"
            f"💼 {ticker}\n"
            f"📊 Продано: {sell_quantity}\n"
            f"💵 Цена входа: ${entry_price:.4f}\n"
            f"💰 Цена продажи: ${sell_price:.4f}\n"
            f"💎 Чистый профит: <b>${profit_usd:.2f}</b> ({profit_pct:+.1f}%)\n\n"
            f"{result_msg}\n"
            f"{history_msg if success_history else '⚠️ ' + history_msg}"
        )

        await message.answer(result_text, parse_mode="HTML")
        await state.clear()

    except ValueError:
        await message.answer("❌ Ошибка: количество и цена должны быть числами")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(ManagePositionState.waiting_for_new_target)
async def process_target_change(message: Message, state: FSMContext):
    """Обработка изменения цели"""
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        data = await state.get_data()
        ticker = data.get("ticker")
        row_num = data.get("row_num")

        new_target = float(message.text.strip())

        # Обновляем в таблице
        loop = asyncio.get_event_loop()
        success, result_msg = await loop.run_in_executor(
            None, update_take_profit, ticker, row_num, new_target
        )

        await message.answer(result_msg, parse_mode="HTML")

        if success:
            await state.clear()

    except ValueError:
        await message.answer("❌ Ошибка: введите число")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

async def main():
    # Запуск утреннего дайджеста в 09:00
    scheduler.add_job(send_daily_digest, "cron", hour=9, minute=0)

    # Запуск проверки волатильности каждые 15 минут
    scheduler.add_job(check_volatility_alerts, "interval", minutes=15)

    scheduler.start()
    print("[OK] Bot zapuschen. Utrenniy didzhest: 09:00. Proverka volatilnosti: kazhdye 15 minut.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
