import os
import json
import asyncio
import logging
import time
from datetime import datetime, timedelta
from functools import wraps
import pandas as pd
import requests
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BotCommand
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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_errors.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Retry декоратор для API запросов
def retry_on_failure(max_retries=3, delay=1, backoff=2):
    """
    Декоратор для повторных попыток при ошибках API
    max_retries: максимальное количество попыток
    delay: начальная задержка в секундах
    backoff: множитель для экспоненциального роста задержки
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            current_delay = delay

            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.RequestException,
                        requests.exceptions.Timeout,
                        requests.exceptions.HTTPError) as e:
                    retries += 1
                    if retries >= max_retries:
                        logger.error(f"Failed after {max_retries} retries in {func.__name__}: {e}")
                        raise

                    logger.warning(f"Retry {retries}/{max_retries} for {func.__name__} after error: {e}")
                    time.sleep(current_delay)
                    current_delay *= backoff
                except Exception as e:
                    logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                    raise

            return None
        return wrapper
    return decorator

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

# --- ПОСТОЯННАЯ КЛАВИАТУРА ---
def get_main_keyboard():
    """Создает постоянную клавиатуру быстрого доступа"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Портфель"), KeyboardButton(text="💰 Профит / PnL")],
            [KeyboardButton(text="✏️ Управление"), KeyboardButton(text="➕ Добавить актив")],
            [KeyboardButton(text="📰 Дайджест"), KeyboardButton(text="🫀 Пульс рынка")],
            [KeyboardButton(text="💎 Alpha-Радар")]
        ],
        resize_keyboard=True,
        persistent=True
    )
    return keyboard

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

# --- ГЛОБАЛЬНАЯ ЗАЩИТА: TOTAL WHITELIST LOCKDOWN ---
class AuthMiddleware(BaseMiddleware):
    """Middleware для полной блокировки доступа неавторизованных пользователей"""
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.id != ADMIN_CHAT_ID:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ запрещен. Бот приватный.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔ Доступ ограничен. Это приватный торговый терминал.")
            return  # Полная блокировка дальнейшего выполнения
        return await handler(event, data)

# Регистрируем middleware для всех сообщений и callback-запросов
dp.message.middleware(AuthMiddleware())
dp.callback_query.middleware(AuthMiddleware())

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
            print("gspread_client не инициализирован")
            return []

        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = spreadsheet.get_worksheet(0)  # Spot_Hold лист (первый лист)
        all_values = worksheet.get_all_values()

        positions = []
        for idx, row in enumerate(all_values[1:], start=2):  # Пропускаем заголовок, начинаем со строки 2
            # Колонка A: Тикер
            if len(row) > 0 and row[0].strip():
                ticker = row[0].strip().upper()

                # Пропускаем заголовок, если он попал в данные
                if ticker.lower() in ["тикер", "ticker"]:
                    continue

                # Колонка B: Количество
                quantity = 0
                if len(row) > 1 and row[1]:
                    try:
                        quantity = float(str(row[1]).replace(",", ".").strip())
                    except ValueError:
                        quantity = 0

                # Колонка C: Точка входа
                entry_price = 0
                if len(row) > 2 and row[2]:
                    try:
                        entry_price = float(str(row[2]).replace("$", "").replace(",", ".").strip())
                    except ValueError:
                        entry_price = 0

                # Колонка D: Тейк-профит
                take_profit = 0
                if len(row) > 3 and row[3]:
                    try:
                        take_profit = float(str(row[3]).replace("$", "").replace(",", ".").strip())
                    except ValueError:
                        take_profit = 0

                positions.append({
                    "row": idx,
                    "ticker": ticker,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "take_profit": take_profit
                })

        print(f"Загружено позиций из Spot_Hold (с номерами строк): {len(positions)}")
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

def get_realized_profit():
    """Получает общую реализованную прибыль из листа История сделок"""
    try:
        worksheet = ensure_profit_history_sheet()
        if not worksheet:
            return 0.0

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:  # Только заголовок или пусто
            return 0.0

        total_profit = 0.0
        for row in all_values[1:]:  # Пропускаем заголовок
            if len(row) > 5 and row[5]:  # Колонка "Профит $"
                try:
                    profit = float(row[5])
                    total_profit += profit
                except ValueError:
                    continue

        return total_profit
    except Exception as e:
        print(f"Error getting realized profit: {e}")
        return 0.0

def get_trade_statistics():
    """Получает расширенную статистику сделок: Win Rate, количество сделок, средний профит"""
    try:
        worksheet = ensure_profit_history_sheet()
        if not worksheet:
            return {"total_trades": 0, "winning_trades": 0, "win_rate": 0.0, "avg_profit": 0.0, "total_profit": 0.0}

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return {"total_trades": 0, "winning_trades": 0, "win_rate": 0.0, "avg_profit": 0.0, "total_profit": 0.0}

        total_trades = 0
        winning_trades = 0
        total_profit = 0.0
        profits = []

        for row in all_values[1:]:  # Пропускаем заголовок
            if len(row) > 5 and row[5]:  # Колонка "Профит $"
                try:
                    profit = float(row[5])
                    total_profit += profit
                    profits.append(profit)
                    total_trades += 1
                    if profit > 0:
                        winning_trades += 1
                except ValueError:
                    continue

        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        avg_profit = (total_profit / total_trades) if total_trades > 0 else 0.0

        return {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "win_rate": win_rate,
            "avg_profit": avg_profit,
            "total_profit": total_profit
        }
    except Exception as e:
        print(f"Error getting trade statistics: {e}")
        return {"total_trades": 0, "winning_trades": 0, "win_rate": 0.0, "avg_profit": 0.0, "total_profit": 0.0}

def get_profit_by_ticker(ticker):
    """Получает суммарную прибыль по конкретному тикеру"""
    try:
        worksheet = ensure_profit_history_sheet()
        if not worksheet:
            return 0.0

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return 0.0

        ticker_profit = 0.0
        for row in all_values[1:]:
            if len(row) > 5 and row[1].strip().upper() == ticker.upper():
                try:
                    profit = float(row[5])
                    ticker_profit += profit
                except ValueError:
                    continue

        return ticker_profit
    except Exception as e:
        print(f"Error getting profit for {ticker}: {e}")
        return 0.0

def calculate_unrealized_pnl(spot_positions, prices_data):
    """Рассчитывает нереализованный PnL для всех открытых позиций"""
    total_unrealized_usd = 0.0
    total_invested = 0.0

    for pos in spot_positions:
        ticker = pos['ticker']
        quantity = pos['quantity']
        entry_price = pos['entry_price']

        if entry_price <= 0 or quantity <= 0:
            continue

        # Получаем текущую цену
        coin_id = COIN_MAP.get(ticker)
        if not coin_id or coin_id not in prices_data:
            continue

        current_price = prices_data[coin_id].get("usd", 0)
        if current_price <= 0:
            continue

        # Расчет прибыли/убытка
        position_cost = entry_price * quantity
        current_value = current_price * quantity
        unrealized = current_value - position_cost

        total_unrealized_usd += unrealized
        total_invested += position_cost

    # Рассчитываем процент
    unrealized_pct = (total_unrealized_usd / total_invested * 100) if total_invested > 0 else 0

    return total_unrealized_usd, unrealized_pct

# Кэш алертов волатильности: {coin_name: last_alert_timestamp}
volatility_alerts_cache = {}

# История алертов волатильности (максимум 50 последних)
volatility_alerts_history = []
MAX_ALERTS_HISTORY = 50

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
        # gid=0 обычно первый лист (Spot_Hold)
        url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv&gid=0"
        df = pd.read_csv(url)

        spot_list = []
        # Начинаем со 2-й строки (индекс 1), пропускаем заголовок
        for idx, row in df.iterrows():
            # Колонка A (индекс 0): Тикер
            ticker = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not ticker or ticker.lower() in ["тикер", "ticker"]:  # Пропускаем заголовок
                continue

            # Колонка B (индекс 1): Количество
            quantity = 0
            if len(row) > 1 and pd.notna(row.iloc[1]):
                try:
                    quantity = float(str(row.iloc[1]).replace(",", ".").strip())
                except ValueError:
                    quantity = 0

            # Колонка C (индекс 2): Точка входа
            entry_price = None
            if len(row) > 2 and pd.notna(row.iloc[2]):
                try:
                    entry_price = float(str(row.iloc[2]).replace("$", "").replace(",", ".").strip())
                except ValueError:
                    pass

            # Колонка D (индекс 3): Тейк-профит
            take_profit = None
            if len(row) > 3 and pd.notna(row.iloc[3]):
                try:
                    take_profit = float(str(row.iloc[3]).replace("$", "").replace(",", ".").strip())
                except ValueError:
                    pass

            # Колонка E (индекс 4): Заметка/Стратегия
            note = ""
            if len(row) > 4 and pd.notna(row.iloc[4]):
                note = str(row.iloc[4]).strip()

            spot_list.append({
                "ticker": ticker.upper(),
                "quantity": quantity,
                "entry_price": entry_price,
                "take_profit": take_profit,
                "note": note
            })

        print(f"Загружено монет из Spot_Hold: {len(spot_list)}")
        return spot_list
    except Exception as e:
        print(f"Ошибка чтения спот-портфеля: {e}")
        return []

# Функция чтения аирдропов из Google Таблицы (лист Airdrop_Radar)
def get_airdrops_activities():
    """Читает активности/аирдропы: [Проект, Тип, Дедлайн, Статус]"""
    try:
        # Пробуем читать второй лист (Airdrop_Radar)
        # Сначала по gid, если не получится - по индексу
        url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv&gid=878500138"
        df = pd.read_csv(url)

        activities_list = []
        for _, row in df.iterrows():
            # Колонка A: Проект
            project = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not project or project.lower() in ["проект", "project"]:  # Пропускаем заголовок
                continue

            # Колонка B: Тип
            activity_type = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else "Аирдроп"

            # Колонка C: Дедлайн
            deadline = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""

            # Колонка D: Статус
            status = str(row.iloc[3]).strip() if len(row) > 3 and pd.notna(row.iloc[3]) else "Активен"

            activities_list.append({
                "project": project,
                "type": activity_type,
                "deadline": deadline,
                "status": status
            })

        print(f"Загружено активностей из Airdrop_Radar: {len(activities_list)}")
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
@retry_on_failure(max_retries=3, delay=2, backoff=2)
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
        logger.error(f"Ошибка получения Fear & Greed Index: {e}")
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

@retry_on_failure(max_retries=3, delay=2, backoff=2)
def get_top_coins_prices():
    """Получает цены топ криптовалют (BTC, ETH, SOL, BNB)"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "bitcoin,ethereum,solana,binancecoin",
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        response = requests.get(url, params=params, timeout=10).json()
        return response
    except Exception as e:
        logger.error(f"Ошибка получения цен топ монет: {e}")
        return {}

@retry_on_failure(max_retries=3, delay=2, backoff=2)
def get_global_market_data():
    """Получает глобальные рыночные данные"""
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10).json()

        if "data" in response:
            data = response["data"]
            return {
                "total_market_cap": data.get("total_market_cap", {}).get("usd", 0),
                "total_market_cap_change_24h": data.get("market_cap_change_percentage_24h_usd", 0),
                "btc_dominance": data.get("market_cap_percentage", {}).get("btc", 0),
                "eth_dominance": data.get("market_cap_percentage", {}).get("eth", 0)
            }
        return None
    except Exception as e:
        logger.error(f"Ошибка получения глобальных данных: {e}")
        return None

def get_eth_gas_price():
    """Получает текущую цену газа Ethereum"""
    try:
        # Используем Etherscan API (можно использовать без ключа для базовых запросов)
        url = "https://api.etherscan.io/api"
        params = {
            "module": "gastracker",
            "action": "gasoracle"
        }
        response = requests.get(url, params=params, timeout=10).json()

        if response.get("status") == "1" and "result" in response:
            gas_price = int(response["result"].get("ProposeGasPrice", 0))
            return gas_price
        return None
    except Exception as e:
        print(f"Ошибка получения ETH Gas: {e}")
        return None

def get_altcoin_season_index():
    """Получает индекс альткоин-сезона"""
    try:
        # Упрощенный расчет на основе доминации BTC
        # Альтсезон: BTC dominance < 40% = индекс высокий
        # BTC сезон: BTC dominance > 60% = индекс низкий
        global_data = get_global_market_data()
        if global_data:
            btc_dom = global_data["btc_dominance"]
            # Формула: (70 - btc_dom) * 2 (примерно)
            # BTC dom 40% -> индекс 60
            # BTC dom 50% -> индекс 40
            # BTC dom 60% -> индекс 20
            index = max(0, min(100, (70 - btc_dom) * 2))
            return int(index)
        return None
    except Exception as e:
        print(f"Ошибка расчета Altcoin Season Index: {e}")
        return None

# --- МОДУЛЬ ALPHA-РАДАР (ПОИСК ГЕМОВ) ---

@retry_on_failure(max_retries=3, delay=2, backoff=2)
def fetch_alpha_candidates():
    """Загружает кандидатов из рангов 101-400 с CoinGecko"""
    try:
        candidates = []

        # Загружаем страницы 2-4 (ранги 101-400, по 100 монет на страницу)
        for page in range(2, 5):
            url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": page,
                "sparkline": "false"
            }

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            candidates.extend(data)
            print(f"Загружено {len(data)} монет со страницы {page}")

        print(f"Всего загружено кандидатов: {len(candidates)}")
        return candidates

    except Exception as e:
        logger.error(f"Ошибка загрузки кандидатов: {e}")
        return []

def get_coin_details(coin_id):
    """Получает детальную информацию о монете для определения сектора"""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Извлекаем категории/теги
        categories = data.get("categories", [])
        return categories
    except Exception as e:
        print(f"Ошибка получения деталей для {coin_id}: {e}")
        return []

# Словарь-справочник известных токенов и их секторов
SECTOR_MAP = {
    "HNT": "DePIN", "AKT": "DePIN", "ATH": "DePIN", "GRASS": "DePIN", "RNDR": "DePIN",
    "RENDER": "DePIN", "FIL": "DePIN", "AR": "DePIN",
    "BOME": "Meme", "PEPE": "Meme", "WIF": "Meme", "BONK": "Meme", "FLOKI": "Meme",
    "DOGE": "Meme", "SHIB": "Meme", "BABYDOGE": "Meme", "ELON": "Meme", "SAMO": "Meme",
    "PROM": "Layer 2", "ARB": "Layer 2", "OP": "Layer 2", "STRK": "Layer 2",
    "MATIC": "Layer 2", "METIS": "Layer 2", "IMX": "Layer 2", "MANTA": "Layer 2",
    "FET": "AI", "NEAR": "AI", "TAO": "AI", "AGIX": "AI", "OCEAN": "AI", "GRT": "AI",
    "ONDO": "RWA", "MKR": "RWA", "POLYX": "RWA", "RIO": "RWA",
    "PENDLE": "DeFi", "AAVE": "DeFi", "UNI": "DeFi", "CRV": "DeFi", "SNX": "DeFi",
    "SOL": "Layer 1", "AVAX": "Layer 1", "ATOM": "Layer 1", "DOT": "Layer 1",
    "SUI": "Layer 1", "APT": "Layer 1", "SEI": "Layer 1", "INJ": "Layer 1"
}

def determine_sector(coin):
    """Определяет сектор монеты: сначала по словарю, затем по метаданным CoinGecko"""
    try:
        coin_id = coin.get("id", "")
        name = coin.get("name", "").lower()
        symbol = coin.get("symbol", "").upper()

        # Проверяем прямой маппинг в словаре
        if symbol in SECTOR_MAP:
            return SECTOR_MAP[symbol]

        # Получаем категории из CoinGecko
        categories = get_coin_details(coin_id)
        categories_lower = [c.lower() if c else "" for c in categories]

        # Проверяем на мемкоин
        if any(x in categories_lower for x in ["meme", "memes", "meme token"]):
            return "Meme"

        # Проверяем остальные секторы
        if any(x in categories_lower for x in ["depin", "internet-of-things", "iot"]):
            return "DePIN"

        if any(x in categories_lower for x in ["artificial-intelligence", "ai", "machine learning"]):
            return "AI"

        if any(x in categories_lower for x in ["real-world-assets", "rwa", "tokenized assets"]):
            return "RWA"

        if any(x in categories_lower for x in ["layer-1", "layer 1", "l1"]):
            return "Layer 1"

        if any(x in categories_lower for x in ["layer-2", "layer 2", "l2", "scaling"]):
            return "Layer 2"

        # Проверяем ключевые слова в имени и символе
        name_and_symbol = (name + " " + symbol).lower()
        if any(kw in name_and_symbol for kw in ["meme", "doge", "inu", "pepe", "shib"]):
            return "Meme"
        if any(kw in name_and_symbol for kw in ["defi", "swap", "dex", "finance"]):
            return "DeFi"

        # Altcoin по умолчанию
        return "Altcoin"

    except Exception as e:
        print(f"Ошибка определения сектора: {e}")
        return "Altcoin"

def filter_alpha_gems(candidates):
    """Применяет фильтры ликвидности, токеномики и активности"""
    filtered = []

    # Черный список: старые токены экосистем 2017-2020 без активной разработки
    blacklist = {
        "ONG", "NEO", "GAS", "QTUM", "EOS", "IOTA", "XVG", "LSK",
        "STRAT", "DCR", "ZEN", "WAVES", "NXT", "ARDR", "DGB"
    }

    for coin in candidates:
        try:
            # Извлекаем данные
            symbol = coin.get("symbol", "").upper()
            coin_id = coin.get("id", "")
            market_cap = coin.get("market_cap", 0)
            volume = coin.get("total_volume", 0)
            circ_supply = coin.get("circulating_supply", 0)
            total_supply = coin.get("total_supply", 0)

            # Фильтр 0: Исключаем зомби-токены
            if symbol in blacklist:
                continue

            # Пропускаем монеты без данных
            if not market_cap or not volume:
                continue

            # Определяем сектор
            sector = determine_sector(coin)
            is_meme = (sector == "Meme")

            # Специальная логика для мемкоинов
            if is_meme:
                # Для мемов: строгий порог по токеномике и объему
                if total_supply and circ_supply:
                    circ_ratio = circ_supply / total_supply
                    if circ_ratio < 0.95:  # Минимум 95% в рынке
                        continue
                else:
                    continue  # Пропускаем если нет данных о supply

                # Минимальный объем $3M для мемов
                if volume < 3_000_000:
                    continue

                # Для мемов разрешаем любую капитализацию в диапазоне
                if market_cap < 20_000_000 or market_cap > 400_000_000:
                    continue

            else:
                # Стандартные фильтры для не-мемов
                # Фильтр 1: Рыночная капитализация $20M - $400M
                if market_cap < 20_000_000 or market_cap > 400_000_000:
                    continue

                # Фильтр 2: Объём торгов > $1.5M
                if volume < 1_500_000:
                    continue

                # Фильтр 4: Токеномика (минимум 40% токенов в циркуляции)
                if total_supply and circ_supply:
                    circ_ratio = circ_supply / total_supply
                    if circ_ratio < 0.40:
                        continue
                else:
                    circ_ratio = None

            # Фильтр 3: Аномалия активности (Volume/MCap >= 0.10)
            vol_mcap_ratio = volume / market_cap
            if vol_mcap_ratio < 0.10:
                continue

            # Монета прошла все фильтры
            coin["vol_mcap_ratio"] = vol_mcap_ratio
            coin["circ_ratio"] = circ_ratio if total_supply and circ_supply else None
            coin["sector"] = sector
            coin["is_meme"] = is_meme
            filtered.append(coin)

        except Exception as e:
            print(f"Ошибка обработки {coin.get('symbol', 'unknown')}: {e}")
            continue

    print(f"После фильтрации осталось: {len(filtered)} монет")
    return filtered

def search_gem_news(token_name, ticker):
    """Ищет актуальные новости о токене через Tavily"""
    try:
        tavily = TavilyClient(api_key=TAVILY_API_KEY)
        search_query = f"{token_name} {ticker} crypto news catalyst updates 2026"
        search_result = tavily.search(query=search_query, max_results=3, topic="news")

        if not search_result or "results" not in search_result:
            return "Свежих новостей не найдено."

        news_text = ""
        for item in search_result["results"]:
            title = item.get("title", "")
            snippet = item.get("content", "")
            news_text += f"- {title}: {snippet}\n"

        return news_text if news_text else "Свежих новостей не найдено."

    except Exception as e:
        print(f"Ошибка поиска новостей для {ticker}: {e}")
        return "Ошибка поиска новостей."

def create_fallback_gem_plan(coin, index):
    """Создает автоматический торговый план если AI недоступен"""
    try:
        name = coin.get("name", "Unknown")
        symbol = coin.get("symbol", "").upper()
        price = coin.get("current_price", 0)
        market_cap = coin.get("market_cap", 0) / 1_000_000
        vol_mcap = coin.get("vol_mcap_ratio", 0) * 100
        circ_ratio = coin.get("circ_ratio", 0) * 100 if coin.get("circ_ratio") else 0
        change_24h = coin.get("price_change_percentage_24h", 0)

        # Получаем сектор из метаданных
        sector = coin.get("sector", "Altcoin")
        is_meme = coin.get("is_meme", False)

        # Автоматический расчет уровней
        entry_from = price * 0.95  # -5%
        entry_to = price * 1.02    # +2%
        stop = price * 0.85        # -15%

        # Динамические TP на основе волатильности
        if abs(change_24h) > 15:
            # Высокая волатильность - консервативные цели
            tp1 = price * 1.4  # +40%
            tp2 = price * 2.0  # +100%
            tp3 = price * 3.0  # +200%
        elif abs(change_24h) > 5:
            # Средняя волатильность
            tp1 = price * 1.5  # +50%
            tp2 = price * 2.2  # +120%
            tp3 = price * 3.5  # +250%
        else:
            # Низкая волатильность - агрессивные цели
            tp1 = price * 1.6  # +60%
            tp2 = price * 2.5  # +150%
            tp3 = price * 4.0  # +300%

        # Динамический драйвер в зависимости от сектора монеты
        if is_meme or sector == "Meme":
            # Для мемкоинов
            driver = f"Аномальный объем торгов ({vol_mcap:.0f}% от капы), 100% циркуляция в рынке, отсутствие инфляционного давления"
        elif sector in ["DePIN", "AI", "Layer 2", "Layer 1"]:
            # Для технологических проектов
            driver = f"Высокий приток ликвидности при отсутствии крупных клифф-разлоков в ближайшие 45 дней. Сетап под накопление"
        elif sector == "RWA":
            driver = f"Институциональный интерес к токенизации активов, Volume/MCap {vol_mcap:.0f}%, чистая токеномика"
        elif sector == "DeFi":
            driver = f"Рост активности протокола, Volume/MCap {vol_mcap:.0f}%, отсутствие крупных анлоков"
        else:
            # Для остальных (Altcoin)
            driver = f"Высокая торговая активность: Volume/MCap {vol_mcap:.0f}%, разлоки чисты на 45+ дней"

        return {
            "name": name,
            "ticker": symbol,
            "sector": sector,
            "driver": driver,
            "price": price,
            "entry_from": entry_from,
            "entry_to": entry_to,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "market_cap": market_cap,
            "vol_mcap": vol_mcap,
            "circ_ratio": circ_ratio,
            "volume_24h": coin.get("total_volume", 0)  # Добавляем 24h volume
        }
    except Exception as e:
        print(f"Ошибка создания fallback плана: {e}")
        return None

def analyze_gems_with_ai(top_gems):
    """Анализирует топ-гемы через LLM и формирует торговый план"""
    try:
        # Отбираем ТОП-5 по Volume/MCap для оптимизации
        top_5 = sorted(top_gems, key=lambda x: x.get("vol_mcap_ratio", 0), reverse=True)[:5]
        print(f"Отобрано ТОП-5 монет по Volume/MCap для анализа")

        # Формируем КРАТКИЕ данные для LLM (только основные метрики)
        gems_data = ""
        for idx, gem in enumerate(top_5, 1):
            name = gem.get("name", "Unknown")
            symbol = gem.get("symbol", "").upper()
            price = gem.get("current_price", 0)
            market_cap = gem.get("market_cap", 0) / 1_000_000
            vol_mcap = gem.get("vol_mcap_ratio", 0) * 100
            change_24h = gem.get("price_change_percentage_24h", 0)
            sector = gem.get("sector", "Altcoin")

            gems_data += f"{idx}. {name} (${symbol}) | Сектор: {sector} | Цена: ${price} | Капа: ${market_cap:.1f}M | Vol/MCap: {vol_mcap:.0f}% | 24ч: {change_24h:+.1f}%\n"

        # Упрощенный промпт для LLM
        prompt = f"""Отбери 3 лучших актива из списка и составь торговый план (R/R >= 1:3).

ЗАПРЕТЫ:
- Не придумывай факты
- Не используй общие фразы ("рост спроса", "развитие")
- Исключай старые токены (NEO, EOS, QTUM)

ДРАЙВЕР (макс 15 слов):
- Для Meme: Аномальный объем торгов (X% от капы), 100% циркуляция, вирусность
- Для DePIN/AI/Layer 2: Высокий приток ликвидности, отсутствие крупных клифф-разлоков в ближайшие 45 дней
- Для остальных: Высокая торговая активность Volume/MCap X%, технический сетап

Формат ответа для каждого актива:

---АКТИВ---
Название: [название]
Тикер: [символ]
Сектор: [используй ТОЧНЫЙ сектор из списка выше]
Драйвер: [факт, макс 15 слов]
Текущая_цена: [число]
Вход_от: [число, -5%]
Вход_до: [число, +2%]
Стоп: [число, -15%]
TP1: [число, +40-70%]
TP2: [число, +100-180%]
TP3: [число, +250-400%]
---КОНЕЦ---

Монеты:
{gems_data}"""

        # Отправляем в OpenRouter с timeout
        print("Отправляю запрос в OpenRouter API...")
        print(f"OPENROUTER_API_KEY present: {bool(OPENROUTER_API_KEY)}")
        try:
            response = openai_client.chat.completions.create(
                model="deepseek/deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000,
                timeout=25.0
            )

            result = response.choices[0].message.content.strip()
            print(f"Получен ответ от AI, длина: {len(result)} символов")
            return result, top_5

        except Exception as api_error:
            print(f"LLM Error: {api_error}")
            print(f"Error type: {type(api_error).__name__}")
            import traceback
            traceback.print_exc()
            return None, top_5

    except Exception as e:
        print(f"Ошибка анализа через AI: {e}")
        import traceback
        traceback.print_exc()
        return None, []

@retry_on_failure(max_retries=3, delay=2, backoff=2)
def get_funding_rate(symbol="BTCUSDT"):
    """Получает ставку фондирования с Binance Futures"""
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        funding_rate = float(data.get("lastFundingRate", 0)) * 100  # Конвертируем в проценты
        return funding_rate
    except Exception as e:
        logger.error(f"Ошибка получения Funding Rate для {symbol}: {e}")
        return None

async def get_market_pulse_text():
    """Формирует расширенный текст рыночного пульса"""
    loop = asyncio.get_event_loop()

    # Получаем все данные параллельно
    fng_data = await loop.run_in_executor(None, get_fear_greed_index)
    top_coins = await loop.run_in_executor(None, get_top_coins_prices)
    global_data = await loop.run_in_executor(None, get_global_market_data)
    alt_season = await loop.run_in_executor(None, get_altcoin_season_index)
    btc_funding = await loop.run_in_executor(None, get_funding_rate, "BTCUSDT")
    eth_funding = await loop.run_in_executor(None, get_funding_rate, "ETHUSDT")

    if not fng_data:
        return "🌡 <b>Рыночный пульс:</b> данные недоступны"

    # Эмодзи для Fear & Greed
    status_emoji = {
        "Extreme Fear": "😱",
        "Fear": "😰",
        "Neutral": "😐",
        "Greed": "😏",
        "Extreme Greed": "🤑"
    }

    emoji = status_emoji.get(fng_data["classification"], "📊")

    # Начало сообщения
    pulse_text = f"🌡 <b>РЫНОЧНЫЙ ПУЛЬС</b>\n\n"
    pulse_text += f"{emoji} <b>Индекс страха и жадности:</b> {fng_data['value']}/100 ({fng_data['classification']})\n\n"

    # Основные активы
    pulse_text += "💎 <b>Основные активы:</b>\n"

    if "bitcoin" in top_coins:
        btc = top_coins["bitcoin"]
        btc_price = btc.get("usd", 0)
        btc_change = btc.get("usd_24h_change", 0)
        change_str = f"+{btc_change:.1f}%" if btc_change >= 0 else f"{btc_change:.1f}%"
        pulse_text += f"₿ Bitcoin: <b>${btc_price:,.0f}</b> ({change_str})\n"

    if "ethereum" in top_coins:
        eth = top_coins["ethereum"]
        eth_price = eth.get("usd", 0)
        eth_change = eth.get("usd_24h_change", 0)
        change_str = f"+{eth_change:.1f}%" if eth_change >= 0 else f"{eth_change:.1f}%"
        pulse_text += f"Ξ Ethereum: <b>${eth_price:,.2f}</b> ({change_str})\n"

    if "solana" in top_coins:
        sol = top_coins["solana"]
        sol_price = sol.get("usd", 0)
        sol_change = sol.get("usd_24h_change", 0)
        change_str = f"+{sol_change:.1f}%" if sol_change >= 0 else f"{sol_change:.1f}%"
        pulse_text += f"◎ Solana: <b>${sol_price:.2f}</b> ({change_str})\n"

    if "binancecoin" in top_coins:
        bnb = top_coins["binancecoin"]
        bnb_price = bnb.get("usd", 0)
        bnb_change = bnb.get("usd_24h_change", 0)
        change_str = f"+{bnb_change:.1f}%" if bnb_change >= 0 else f"{bnb_change:.1f}%"
        pulse_text += f"🔶 BNB: <b>${bnb_price:.2f}</b> ({change_str})\n"

    # Рыночные индикаторы
    pulse_text += "\n📊 <b>Рыночные индикаторы:</b>\n"

    if global_data:
        # BTC Dominance
        btc_dom = global_data["btc_dominance"]
        pulse_text += f"🔸 BTC Dominance: <b>{btc_dom:.1f}%</b>\n"

        # Total Market Cap
        total_cap = global_data["total_market_cap"]
        cap_change = global_data["total_market_cap_change_24h"]
        cap_trillion = total_cap / 1_000_000_000_000
        change_str = f"+{cap_change:.1f}%" if cap_change >= 0 else f"{cap_change:.1f}%"
        pulse_text += f"🔸 Total Market Cap: <b>${cap_trillion:.2f}T</b> ({change_str})\n"

    # Altcoin Season Index
    if alt_season is not None:
        if alt_season >= 75:
            season_status = "🌊 Альтсезон!"
        elif alt_season >= 50:
            season_status = "📈 Альты растут"
        elif alt_season >= 25:
            season_status = "⚖️ Нейтрально"
        else:
            season_status = "₿ BTC сезон"
        pulse_text += f"🔸 Altcoin Season Index: <b>{alt_season}/100</b> ({season_status})\n"

    # Funding Rate с динамическим статусом
    if btc_funding is not None:
        funding_sign = "+" if btc_funding >= 0 else ""

        # Определяем эмодзи и статус по уровням
        if btc_funding >= 0.03:
            funding_emoji = "🔴"
            funding_status = "Критический перегрев лонгами"
        elif 0.01 < btc_funding < 0.03:
            funding_emoji = "🟡"
            funding_status = "Умеренный бычий оптимизм"
        elif 0.00 <= btc_funding <= 0.01:
            funding_emoji = "🟢"
            funding_status = "Нейтрально"
        else:  # funding < 0.00
            funding_emoji = "🟣"
            funding_status = "Перекос в шорты / Риск Short Squeeze"

        pulse_text += f"🔸 Funding Rate (BTC): <b>{funding_sign}{btc_funding:.4f}%</b> {funding_emoji} ({funding_status})\n"

    # Трейдерский вердикт (объединяем сигнал и анализ)
    pulse_text += "\n💡 <b>Трейдерский вердикт:</b>\n"

    if global_data and fng_data:
        cap_change = global_data["total_market_cap_change_24h"]
        btc_dom = global_data["btc_dominance"]
        fear_value = fng_data["value"]

        # Формируем краткий, емкий анализ без дублирования
        if cap_change < -4:
            pulse_text += f"Сильное падение рынка ({cap_change:.1f}%). "
        elif cap_change < -2:
            pulse_text += f"Локальная коррекция ({cap_change:.1f}%) "
        elif cap_change < 0:
            pulse_text += f"Легкая просадка ({cap_change:.1f}%). "
        elif cap_change < 2:
            pulse_text += f"Умеренный рост (+{cap_change:.1f}%). "
        elif cap_change < 5:
            pulse_text += f"Сильный рост (+{cap_change:.1f}%). "
        else:
            pulse_text += f"Мощный памп (+{cap_change:.1f}%)! "

        # Анализ доминации (без повторов)
        if btc_dom > 60:
            pulse_text += f"при очень высокой доминации BTC ({btc_dom:.1f}%). "
            pulse_text += "Ликвидность массово удерживается в биткоине, альткоины под сильным давлением. "
            pulse_text += "Агрессивный набор альтов преждевременен."
        elif btc_dom > 55:
            pulse_text += f"при высокой доминации BTC ({btc_dom:.1f}%). "
            pulse_text += "Ликвидность удерживается в биткоине, альткоины под давлением. "
            pulse_text += "Агрессивный набор альтов преждевременен."
        elif btc_dom > 45:
            pulse_text += f"при нормальной доминации BTC ({btc_dom:.1f}%). "
            pulse_text += "Сбалансированное распределение капитала. "
        elif btc_dom > 40:
            pulse_text += f"при снижающейся доминации BTC ({btc_dom:.1f}%). "
            pulse_text += "Деньги начинают перетекать в альткоины — подготовка к альтсезону."
        else:
            pulse_text += f"при низкой доминации BTC ({btc_dom:.1f}%). "
            pulse_text += "Полноценный альтсезон! Деньги активно идут в альткоины."

        # Предупреждение на основе экстремального фандинга
        if btc_funding is not None:
            if btc_funding >= 0.03:
                pulse_text += "\n\n⚠️ <b>Внимание:</b> сильный перегрев рынка лонгами. Повышенная вероятность сквиза вниз для выноса плечей."
            elif btc_funding < 0.00:
                pulse_text += "\n\n⚡️ <b>Внимание:</b> доминируют агрессивные шорты. Высокий потенциал резкого шорт-сквиза вверх."

        # Парадокс Fear & Greed
        if (fear_value > 60 and cap_change < -3) or (fear_value < 40 and cap_change > 3):
            pulse_text += "\n\n⚠️ <b>Парадокс:</b> "
            if fear_value > 60 and cap_change < -3:
                pulse_text += f"Индекс показывает жадность ({fear_value}/100), но рынок падает. "
                pulse_text += "Люди ещё не паникуют и держат позиции. Возможно дальнейшее падение."
            else:
                pulse_text += f"Индекс показывает страх ({fear_value}/100), но рынок растёт! "
                pulse_text += "Умные деньги входят, пока толпа боится."

        # План действий
        pulse_text += "\n\n🎯 <b>План действий:</b>\n"

        if cap_change < -3:
            pulse_text += "• ⏸️ Воздержаться от импульсивных лонгов по альтам\n"
            pulse_text += "• 👀 Ждать разворота Total MCap (+1.5-2%) и падения доминации BTC &lt; 55%\n"
            pulse_text += "• 📋 Подготовить пул сильных монет в Alpha-Радаре под отскок\n"
            # Дополнение на основе фандинга
            if btc_funding is not None and btc_funding >= 0.03:
                pulse_text += "• 🚨 Экстремальный фандинг — не открывать новые лонги с плечом"
        elif cap_change < 0:
            pulse_text += "• ⏸️ Коррекция — можно переждать\n"
            pulse_text += "• 👀 Следить за Total Market Cap: разворот на +2% = сигнал к входу\n"
            pulse_text += "• 💰 Готовить список монет для покупки\n"
            # Дополнение на основе фандинга
            if btc_funding is not None and btc_funding < 0.00:
                pulse_text += "• ⚡ Негативный фандинг — возможен резкий шорт-сквиз при развороте"
        elif cap_change > 3 and btc_dom < 50:
            pulse_text += "• 🚀 Сильный рост + низкая BTC доминация = альтсезон!\n"
            pulse_text += "• ✅ Можно докупать качественные альты\n"
            pulse_text += "• ⚠️ Следить за перегревом (Fear &amp; Greed &gt; 80)\n"
            # Дополнение на основе фандинга
            if btc_funding is not None and btc_funding >= 0.03:
                pulse_text += "• 🚨 Осторожно: критический фандинг — риск резкой коррекции"
        else:
            pulse_text += "• 📊 Рынок в нормальном состоянии\n"
            pulse_text += "• ✅ Можно входить в позиции постепенно\n"
            pulse_text += "• 📈 Проверяй /pulse 2-3 раза в день"

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

                # Добавляем в историю алертов
                alert_record = {
                    "timestamp": now,
                    "coin": coin_name,
                    "change_24h": change24h,
                    "price": curr_price,
                    "direction": "pump" if change24h > 0 else "dump"
                }
                volatility_alerts_history.append(alert_record)

                # Ограничиваем размер истории
                if len(volatility_alerts_history) > MAX_ALERTS_HISTORY:
                    volatility_alerts_history.pop(0)

                print(f"Алерт отправлен для {coin_name}: {change24h:+.2f}%")
            except Exception as e:
                print(f"Ошибка отправки алерта для {coin_name}: {e}")

        await asyncio.sleep(1)

# Кэш предыдущих значений рынка для отслеживания разворота
market_trend_cache = {
    "last_cap_change": None,
    "last_btc_dom": None,
    "last_check_time": None
}

async def check_market_trend_reversal():
    """Проверяет разворот тренда рынка и отправляет алерты"""
    global market_trend_cache
    loop = asyncio.get_event_loop()

    try:
        # Получаем текущие данные рынка
        global_data = await loop.run_in_executor(None, get_global_market_data)

        if not global_data:
            return

        cap_change = global_data["total_market_cap_change_24h"]
        btc_dom = global_data["btc_dominance"]
        now = datetime.now()

        # Пропускаем первую проверку (нужны предыдущие значения)
        if market_trend_cache["last_cap_change"] is None:
            market_trend_cache["last_cap_change"] = cap_change
            market_trend_cache["last_btc_dom"] = btc_dom
            market_trend_cache["last_check_time"] = now
            return

        last_cap_change = market_trend_cache["last_cap_change"]
        last_btc_dom = market_trend_cache["last_btc_dom"]

        # Проверяем разворот с падения на рост
        if last_cap_change < -2 and cap_change > 1:
            alert_text = (
                "🔔 <b>РАЗВОРОТ ТРЕНДА!</b>\n\n"
                f"📈 Total Market Cap развернулся:\n"
                f"Было: <b>{last_cap_change:.1f}%</b> (падение)\n"
                f"Стало: <b>+{cap_change:.1f}%</b> (рост)\n\n"
                f"✅ Это может быть сигнал к входу в рынок!\n"
                f"Проверь /pulse для полной картины."
            )
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=alert_text, parse_mode="HTML")
            print(f"Алерт разворота отправлен: {last_cap_change:.1f}% → {cap_change:.1f}%")

        # Проверяем снижение BTC доминации (начало альтсезона)
        if last_btc_dom > 55 and btc_dom < 53:
            alert_text = (
                "🌊 <b>НАЧАЛО АЛЬТСЕЗОНА?</b>\n\n"
                f"📉 BTC Dominance снижается:\n"
                f"Было: <b>{last_btc_dom:.1f}%</b>\n"
                f"Стало: <b>{btc_dom:.1f}%</b>\n\n"
                f"💰 Деньги начинают перетекать в альткоины!\n"
                f"Проверь /pulse для деталей."
            )
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=alert_text, parse_mode="HTML")
            print(f"Алерт альтсезона отправлен: BTC dom {last_btc_dom:.1f}% → {btc_dom:.1f}%")

        # Проверяем резкий рост BTC доминации (бегство в BTC)
        if last_btc_dom < 55 and btc_dom > 58:
            alert_text = (
                "⚠️ <b>БЕГСТВО В БИТКОИН!</b>\n\n"
                f"📈 BTC Dominance резко растет:\n"
                f"Было: <b>{last_btc_dom:.1f}%</b>\n"
                f"Стало: <b>{btc_dom:.1f}%</b>\n\n"
                f"🔴 Деньги массово уходят из альтов!\n"
                f"Осторожность с альткоинами."
            )
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=alert_text, parse_mode="HTML")
            print(f"Алерт бегства в BTC отправлен: BTC dom {last_btc_dom:.1f}% → {btc_dom:.1f}%")

        # Обновляем кэш
        market_trend_cache["last_cap_change"] = cap_change
        market_trend_cache["last_btc_dom"] = btc_dom
        market_trend_cache["last_check_time"] = now

    except Exception as e:
        print(f"Ошибка проверки разворота тренда: {e}")

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

    # Инлайн-меню с кнопками
    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Портфель", callback_data="portfolio")],
        [InlineKeyboardButton(text="💰 Чистый профит / Статистика", callback_data="pnl")],
        [InlineKeyboardButton(text="💎 Alpha-Радар", callback_data="cmd_gems")],
        [InlineKeyboardButton(text="✏️ Управление активами", callback_data="manage_assets")],
        [InlineKeyboardButton(text="📰 Дайджест дедлайнов", callback_data="digest")],
        [InlineKeyboardButton(text="🌡 Пульс рынка", callback_data="pulse")],
        [InlineKeyboardButton(text="➕ Добавить актив", callback_data="add_asset")]
    ])

    await message.answer(
        f"Привет! Твой крипто-терминал обновлен.\n\n"
        f"Доступные команды:\n"
        f"/portfolio - быстрый просмотр портфеля\n"
        f"/pnl - статистика и чистый профит\n"
        f"/gems - альфа-радар (поиск перспективных монет и мемов)\n"
        f"/manage - управление активами\n"
        f"/digest - дайджест новостей и дедлайнов\n"
        f"/check - полный аудит (цены + новости)\n"
        f"/pulse - проверить рыночный пульс\n"
        f"/add - добавить актив в таблицу\n\n"
        f"Или используй кнопки ниже:",
        reply_markup=inline_keyboard
    )

    # Отправляем постоянную клавиатуру
    await message.answer(
        "📱 Используйте меню быстрого доступа:",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("portfolio"))
async def portfolio_cmd(message: Message):
    await send_portfolio_view()

@dp.message(Command("digest"))
async def digest_cmd(message: Message):
    await send_news_digest()

@dp.message(Command("check"))
async def manual_check(message: Message):
    await send_daily_digest()

@dp.message(Command("pulse"))
async def pulse_cmd(message: Message):

    pulse_text = await get_market_pulse_text()
    await message.answer(pulse_text, parse_mode="HTML")

@dp.message(Command("pnl"))
async def pnl_cmd(message: Message):
    """Команда для отображения чистого профита и статистики"""

    if not gspread_client:
        await message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    await message.answer("📊 Рассчитываю статистику портфеля...")

    loop = asyncio.get_event_loop()

    # Получаем расширенную статистику сделок
    trade_stats = await loop.run_in_executor(None, get_trade_statistics)

    # Получаем спот-позиции и актуальные цены
    spot_positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
    prices_data = await loop.run_in_executor(None, fetch_all_prices)

    # Рассчитываем нереализованный PnL
    unrealized_usd, unrealized_pct = await loop.run_in_executor(
        None, calculate_unrealized_pnl, spot_positions, prices_data
    )

    # Общая прибыль
    realized_profit = trade_stats["total_profit"]
    total_profit = realized_profit + unrealized_usd

    # Рассчитываем общий ROI портфеля
    total_invested = sum(pos['entry_price'] * pos['quantity'] for pos in spot_positions if pos['entry_price'] > 0 and pos['quantity'] > 0)
    portfolio_roi = (total_profit / total_invested * 100) if total_invested > 0 else 0

    # Форматирование
    realized_str = f"+${realized_profit:.2f}" if realized_profit >= 0 else f"-${abs(realized_profit):.2f}"
    unrealized_str = f"+${unrealized_usd:.2f}" if unrealized_usd >= 0 else f"-${abs(unrealized_usd):.2f}"
    unrealized_pct_str = f"+{unrealized_pct:.1f}%" if unrealized_pct >= 0 else f"{unrealized_pct:.1f}%"
    total_str = f"+${total_profit:.2f}" if total_profit >= 0 else f"-${abs(total_profit):.2f}"
    roi_str = f"+{portfolio_roi:.1f}%" if portfolio_roi >= 0 else f"{portfolio_roi:.1f}%"

    # Эмодзи в зависимости от результата
    emoji = "🟢" if total_profit >= 0 else "🔴"

    pnl_text = (
        f"📊 <b>Сводка доходности портфеля:</b>\n\n"
        f"💰 Реализованная прибыль: <b>{realized_str}</b>\n"
        f"📈 Плавающий PnL: <b>{unrealized_str}</b> ({unrealized_pct_str})\n"
        f"{emoji} <b>Всего заработано: {total_str}</b>\n"
        f"📊 <b>Общий ROI: {roi_str}</b>\n\n"
        f"📉 <b>Статистика сделок:</b>\n"
        f"🔢 Всего сделок: <b>{trade_stats['total_trades']}</b>\n"
        f"✅ Прибыльных: <b>{trade_stats['winning_trades']}</b>\n"
        f"❌ Убыточных: <b>{trade_stats['losing_trades']}</b>\n"
        f"🎯 Win Rate: <b>{trade_stats['win_rate']:.1f}%</b>\n"
        f"💵 Средний профит: <b>${trade_stats['avg_profit']:.2f}</b>"
    )

    await message.answer(pnl_text, parse_mode="HTML")

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    """Команда для просмотра истории алертов волатильности"""

    if not volatility_alerts_history:
        await message.answer("📭 История алертов пуста. Алерты появятся при обнаружении волатильности ≥15%.")
        return

    # Берём последние 5 алертов
    recent_alerts = volatility_alerts_history[-5:]
    recent_alerts.reverse()  # От новых к старым

    alerts_text = "🔔 <b>История алертов волатильности</b>\n\n"

    for idx, alert in enumerate(recent_alerts, 1):
        timestamp = alert["timestamp"]
        coin = alert["coin"]
        change = alert["change_24h"]
        price = alert["price"]
        direction_emoji = "🚀" if alert["direction"] == "pump" else "📉"

        # Форматируем время
        time_str = timestamp.strftime("%d.%m.%Y %H:%M")
        change_str = f"+{change:.2f}%" if change > 0 else f"{change:.2f}%"

        alerts_text += (
            f"{idx}. {direction_emoji} <b>{coin.upper()}</b>\n"
            f"   Изменение: <b>{change_str}</b>\n"
            f"   Цена: ${price:.6f}\n"
            f"   Время: {time_str}\n\n"
        )

    alerts_text += f"📊 Всего алертов в истории: <b>{len(volatility_alerts_history)}</b>"

    await message.answer(alerts_text, parse_mode="HTML")

@dp.message(Command("summary"))
async def summary_cmd(message: Message):
    """Быстрая сводка портфеля: Total Value, PnL, Best/Worst performers"""

    if not gspread_client:
        await message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    await message.answer("📊 Загружаю сводку портфеля...")

    loop = asyncio.get_event_loop()

    # Получаем данные параллельно
    spot_positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
    prices_data = await loop.run_in_executor(None, fetch_all_prices)
    trade_stats = await loop.run_in_executor(None, get_trade_statistics)

    if not spot_positions:
        await message.answer("📭 Портфель пуст")
        return

    # Рассчитываем метрики для каждой позиции
    positions_with_pnl = []
    total_invested = 0.0
    total_current_value = 0.0

    for pos in spot_positions:
        ticker = pos['ticker']
        quantity = pos['quantity']
        entry_price = pos['entry_price']

        if entry_price <= 0 or quantity <= 0:
            continue

        coin_id = COIN_MAP.get(ticker)
        if not coin_id or coin_id not in prices_data:
            continue

        current_price = prices_data[coin_id].get("usd", 0)
        if current_price <= 0:
            continue

        position_cost = entry_price * quantity
        current_value = current_price * quantity
        unrealized_pnl = current_value - position_cost
        pnl_pct = (unrealized_pnl / position_cost * 100) if position_cost > 0 else 0

        total_invested += position_cost
        total_current_value += current_value

        positions_with_pnl.append({
            "ticker": ticker,
            "pnl_usd": unrealized_pnl,
            "pnl_pct": pnl_pct,
            "current_value": current_value
        })

    # Общий unrealized PnL
    unrealized_pnl = total_current_value - total_invested
    unrealized_pnl_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0

    # Realized profit
    realized_profit = trade_stats["total_profit"]

    # Total profit
    total_profit = realized_profit + unrealized_pnl

    # Находим Best/Worst performers
    if positions_with_pnl:
        best_performer = max(positions_with_pnl, key=lambda x: x["pnl_pct"])
        worst_performer = min(positions_with_pnl, key=lambda x: x["pnl_pct"])
    else:
        best_performer = worst_performer = None

    # Форматирование
    total_value_str = f"${total_current_value:,.2f}"
    unrealized_str = f"+${unrealized_pnl:.2f}" if unrealized_pnl >= 0 else f"-${abs(unrealized_pnl):.2f}"
    unrealized_pct_str = f"+{unrealized_pnl_pct:.1f}%" if unrealized_pnl_pct >= 0 else f"{unrealized_pnl_pct:.1f}%"
    realized_str = f"+${realized_profit:.2f}" if realized_profit >= 0 else f"-${abs(realized_profit):.2f}"
    total_profit_str = f"+${total_profit:.2f}" if total_profit >= 0 else f"-${abs(total_profit):.2f}"

    summary_text = (
        f"📊 <b>СВОДКА ПОРТФЕЛЯ</b>\n\n"
        f"💼 Общая стоимость: <b>{total_value_str}</b>\n"
        f"📈 Unrealized PnL: <b>{unrealized_str}</b> ({unrealized_pct_str})\n"
        f"💰 Realized Profit: <b>{realized_str}</b>\n"
        f"🎯 Total Profit: <b>{total_profit_str}</b>\n\n"
    )

    if best_performer:
        best_pnl_str = f"+{best_performer['pnl_pct']:.1f}%" if best_performer['pnl_pct'] >= 0 else f"{best_performer['pnl_pct']:.1f}%"
        summary_text += f"🚀 Best: <b>{best_performer['ticker']}</b> ({best_pnl_str})\n"

    if worst_performer:
        worst_pnl_str = f"+{worst_performer['pnl_pct']:.1f}%" if worst_performer['pnl_pct'] >= 0 else f"{worst_performer['pnl_pct']:.1f}%"
        summary_text += f"📉 Worst: <b>{worst_performer['ticker']}</b> ({worst_pnl_str})\n"

    summary_text += f"\n🎲 Win Rate: <b>{trade_stats['win_rate']:.1f}%</b> ({trade_stats['winning_trades']}/{trade_stats['total_trades']})"

    await message.answer(summary_text, parse_mode="HTML")

@dp.message(Command("gems"))
async def gems_cmd(message: Message):
    """Команда Alpha-Радар - поиск перспективных гемов"""

    await message.answer("💎 <b>ALPHA-РАДАР</b>\n\n🔍 Сканирую рынок монет вне топ-100...", parse_mode="HTML")

    loop = asyncio.get_event_loop()

    try:
        # Шаг 1: Загрузка кандидатов
        candidates = await loop.run_in_executor(None, fetch_alpha_candidates)

        if not candidates:
            await message.answer("❌ Не удалось загрузить данные с CoinGecko")
            return

        await message.answer(f"✅ Загружено {len(candidates)} кандидатов. Применяю фильтры...")

        # Шаг 2: Фильтрация
        filtered = await loop.run_in_executor(None, filter_alpha_gems, candidates)

        if not filtered:
            await message.answer("❌ Ни одна монета не прошла фильтры")
            return

        await message.answer(f"✅ {len(filtered)} монет прошли фильтры. Анализирую через AI...")

        # Шаг 3: Берём топ-5 по активности для анализа (уже делается внутри analyze_gems_with_ai)

        # Шаг 4: Анализ через AI с fallback
        ai_result = await loop.run_in_executor(None, analyze_gems_with_ai, filtered)

        if ai_result:
            ai_analysis, top_gems = ai_result
        else:
            ai_analysis, top_gems = None, []

        # Fallback: если AI не сработал, формируем сигналы автоматически
        if not ai_analysis:
            await message.answer("⚠️ AI временно недоступен. Формирую сигналы автоматически на основе Volume/MCap...")

            # Берем ТОП-3 по Volume/MCap
            top_3 = sorted(filtered, key=lambda x: x.get("vol_mcap_ratio", 0), reverse=True)[:3]

            gems = []
            for idx, coin in enumerate(top_3, 1):
                gem_plan = create_fallback_gem_plan(coin, idx)
                if gem_plan:
                    gems.append(gem_plan)

            if not gems:
                await message.answer("❌ Не удалось сформировать сигналы")
                return
        else:
            # Выводим сырой ответ AI для отладки
            print("=" * 50)
            print("ОТВЕТ AI:")
            print(ai_analysis)
            print("=" * 50)

            # Шаг 5: Парсинг и форматирование результата
            gems = parse_ai_gems_response(ai_analysis, top_gems)

            if not gems:
                await message.answer("⚠️ Ошибка парсинга AI. Использую автоматический режим...")

                # Fallback на автоматические сигналы
                top_3 = sorted(filtered, key=lambda x: x.get("vol_mcap_ratio", 0), reverse=True)[:3]
                gems = []
                for idx, coin in enumerate(top_3, 1):
                    gem_plan = create_fallback_gem_plan(coin, idx)
                    if gem_plan:
                        gems.append(gem_plan)

                if not gems:
                    await message.answer("❌ Не удалось сформировать сигналы")
                    return

        # Шаг 6: Отправка результатов
        await message.answer("💎 <b>ALPHA-РАДАР | ПОИСК АСИММЕТРИИ (GEMS)</b>\n", parse_mode="HTML")

        for idx, gem in enumerate(gems, 1):
            gem_text = format_gem_message(idx, gem)

            # Кнопки под каждой карточкой
            # Используем текущую цену вместо entry_from для упрощения callback_data
            price_str = str(gem.get('price', 0))
            tp_str = str(gem.get('tp2', 0))

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➕ Добавить в портфель",
                        callback_data=f"addgem_{gem['ticker']}_{price_str}_{tp_str}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📈 DexScreener",
                        url=f"https://dexscreener.com/search?q={gem['ticker']}"
                    )
                ]
            ])

            await message.answer(gem_text, reply_markup=keyboard, parse_mode="HTML")
            await asyncio.sleep(1)

    except Exception as e:
        await message.answer(f"❌ Ошибка выполнения Alpha-Радара: {str(e)}")
        print(f"Ошибка в gems_cmd: {e}")

def parse_ai_gems_response(ai_text, top_gems):
    """Парсит ответ AI и извлекает данные по гемам"""
    gems = []
    blocks = ai_text.split("---АКТИВ---")

    print(f"Найдено блоков: {len(blocks)}")

    for idx, block in enumerate(blocks[1:], 1):  # Пропускаем первый пустой блок
        print(f"\nОбработка блока {idx}:")
        print(block[:200])  # Первые 200 символов для отладки

        if "---КОНЕЦ---" not in block:
            print(f"Блок {idx}: пропущен (нет маркера ---КОНЕЦ---)")
            continue

        try:
            gem_data = {}
            # Извлекаем текст между ---АКТИВ--- и ---КОНЕЦ---
            content = block.split("---КОНЕЦ---")[0].strip()
            lines = content.split("\n")

            for line in lines:
                line = line.strip()
                if not line or ":" not in line:
                    continue

                # Разделяем по первому двоеточию
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue

                key = parts[0].strip()
                value = parts[1].strip()

                if key == "Название":
                    gem_data["name"] = value
                elif key == "Тикер":
                    gem_data["ticker"] = value.upper()
                elif key == "Сектор":
                    gem_data["sector"] = value
                elif key == "Драйвер":
                    gem_data["driver"] = value
                elif key == "Текущая_цена":
                    try:
                        gem_data["price"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга цены: {value}")
                elif key == "Вход_от":
                    try:
                        gem_data["entry_from"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга entry_from: {value}")
                elif key == "Вход_до":
                    try:
                        gem_data["entry_to"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга entry_to: {value}")
                elif key == "Стоп":
                    try:
                        gem_data["stop"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга stop: {value}")
                elif key == "TP1":
                    try:
                        gem_data["tp1"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга TP1: {value}")
                elif key == "TP2":
                    try:
                        gem_data["tp2"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга TP2: {value}")
                elif key == "TP3":
                    try:
                        gem_data["tp3"] = float(value.replace(",", "."))
                    except ValueError:
                        print(f"Ошибка парсинга TP3: {value}")

            # Ищем данные монеты из исходного списка
            if "ticker" in gem_data:
                for coin in top_gems:
                    if coin.get("symbol", "").upper() == gem_data.get("ticker"):
                        gem_data["market_cap"] = coin.get("market_cap", 0) / 1_000_000
                        gem_data["vol_mcap"] = coin.get("vol_mcap_ratio", 0) * 100
                        gem_data["circ_ratio"] = coin.get("circ_ratio", 0) * 100 if coin.get("circ_ratio") else 0
                        gem_data["volume_24h"] = coin.get("total_volume", 0)  # Добавляем 24h volume
                        break

            # Проверяем что все обязательные поля заполнены
            required_fields = ["name", "ticker", "sector", "driver", "price", "entry_from", "entry_to", "stop", "tp1", "tp2", "tp3"]
            missing_fields = [f for f in required_fields if f not in gem_data]

            if missing_fields:
                print(f"Блок {idx}: пропущен (отсутствуют поля: {missing_fields})")
                print(f"Собранные данные: {gem_data}")
            else:
                print(f"Блок {idx}: успешно распарсен ({gem_data['ticker']})")
                gems.append(gem_data)

        except Exception as e:
            print(f"Ошибка парсинга блока {idx}: {e}")
            import traceback
            traceback.print_exc()
            continue

    print(f"\nИтого успешно распарсено: {len(gems)} активов")
    return gems

def format_price(price):
    """Умное округление цен: 2 знака если >= $1, иначе 4-5 значащих цифр"""
    if price >= 1:
        return f"{price:.2f}"
    elif price >= 0.01:
        # Для цен от $0.01 до $0.99 — 4 значащих цифры
        return f"{price:.4f}".rstrip('0').rstrip('.')
    else:
        # Для цен < $0.01 — 5 значащих цифр
        return f"{price:.5f}".rstrip('0').rstrip('.')

def format_gem_message(index, gem):
    """Форматирует сообщение для одного гема"""
    price = gem.get("price", 0)
    mcap = gem.get("market_cap", 0)
    vol_mcap = gem.get("vol_mcap", 0)
    circ = gem.get("circ_ratio", 0)
    volume_24h = gem.get("volume_24h", 0)  # Добавляем 24h volume

    entry_from = gem.get("entry_from", 0)
    entry_to = gem.get("entry_to", 0)
    stop = gem.get("stop", 0)
    tp1 = gem.get("tp1", 0)
    tp2 = gem.get("tp2", 0)
    tp3 = gem.get("tp3", 0)

    # Рассчитываем проценты
    stop_pct = ((stop - price) / price * 100) if price > 0 else 0
    tp1_pct = ((tp1 - price) / price * 100) if price > 0 else 0
    tp2_pct = ((tp2 - price) / price * 100) if price > 0 else 0
    tp3_pct = ((tp3 - price) / price * 100) if price > 0 else 0

    # Risk/Reward
    risk = abs(stop_pct)
    reward = tp2_pct
    rr = f"1:{reward/risk:.1f}" if risk > 0 else "N/A"

    # Форматируем цены с умным округлением
    price_str = format_price(price)
    entry_from_str = format_price(entry_from)
    entry_to_str = format_price(entry_to)
    stop_str = format_price(stop)
    tp1_str = format_price(tp1)
    tp2_str = format_price(tp2)
    tp3_str = format_price(tp3)

    # Форматируем volume
    volume_str = f"${volume_24h/1_000_000:.1f}M" if volume_24h >= 1_000_000 else f"${volume_24h/1_000:.0f}K"

    text = f"""🔥 <b>{index}. [{gem.get('sector', 'Crypto')}] {gem.get('name', 'Unknown')} (${gem.get('ticker', '')})</b>

💵 Текущая: <b>${price_str}</b> | Капа: ${mcap:.0f}M
📊 24h Volume: <b>{volume_str}</b> | Vol/MCap: {vol_mcap:.0f}%
🛡 Токеномика: В рынке {circ:.0f}% | Разлоки: Чисто на 45+ дней
💡 Драйвер: {gem.get('driver', 'Нет данных')}

🎯 <b>ТОРГОВЫЙ ПЛАН (R/R {rr}):</b>
🟢 Набор: ${entry_from_str} – ${entry_to_str}
🛑 Стоп / Отмена: ${stop_str} ({stop_pct:.0f}%)
🎯 TP1: ${tp1_str} (+{tp1_pct:.0f}%) | TP2: ${tp2_str} (+{tp2_pct:.0f}%) | 🚀 TP3: ${tp3_str} (+{tp3_pct:.0f}%)
──────────────────────────────"""

    return text

@dp.message(Command("add"))
async def add_cmd(message: Message):
    """Команда для добавления актива"""

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

# --- ОБРАБОТЧИКИ ТЕКСТОВЫХ КНОПОК ПОСТОЯННОЙ КЛАВИАТУРЫ ---
@dp.message(F.text == "📊 Портфель")
async def keyboard_portfolio(message: Message):
    """Обработчик кнопки 'Портфель'"""
    await send_portfolio_view()

@dp.message(F.text == "💰 Профит / PnL")
async def keyboard_pnl(message: Message):
    """Обработчик кнопки 'Профит / PnL'"""

    if not gspread_client:
        await message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    await message.answer("📊 Рассчитываю статистику портфеля...")

    loop = asyncio.get_event_loop()
    realized_profit = await loop.run_in_executor(None, get_realized_profit)
    spot_positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
    prices_data = await loop.run_in_executor(None, fetch_all_prices)
    unrealized_usd, unrealized_pct = await loop.run_in_executor(
        None, calculate_unrealized_pnl, spot_positions, prices_data
    )

    total_profit = realized_profit + unrealized_usd
    realized_str = f"+${realized_profit:.2f}" if realized_profit >= 0 else f"-${abs(realized_profit):.2f}"
    unrealized_str = f"+${unrealized_usd:.2f}" if unrealized_usd >= 0 else f"-${abs(unrealized_usd):.2f}"
    unrealized_pct_str = f"+{unrealized_pct:.1f}%" if unrealized_pct >= 0 else f"{unrealized_pct:.1f}%"
    total_str = f"+${total_profit:.2f}" if total_profit >= 0 else f"-${abs(total_profit):.2f}"
    emoji = "🟢" if total_profit >= 0 else "🔴"

    pnl_text = (
        f"📊 <b>Сводка доходности портфеля:</b>\n\n"
        f"💰 Реализованная чистая прибыль: <b>{realized_str}</b>\n"
        f"📈 Плавающий PnL (открытые позиции): <b>{unrealized_str}</b> ({unrealized_pct_str})\n"
        f"{emoji} <b>Всего заработано: {total_str}</b>"
    )

    await message.answer(pnl_text, parse_mode="HTML")

@dp.message(F.text == "✏️ Управление")
async def keyboard_manage(message: Message):
    """Обработчик кнопки 'Управление'"""
    await manage_cmd(message)

@dp.message(F.text == "➕ Добавить актив")
async def keyboard_add(message: Message):
    """Обработчик кнопки 'Добавить актив'"""
    await add_cmd(message)

@dp.message(F.text == "📰 Дайджест")
async def keyboard_digest(message: Message):
    """Обработчик кнопки 'Дайджест'"""
    await send_news_digest()

@dp.message(F.text == "🫀 Пульс рынка")
async def keyboard_pulse(message: Message):
    """Обработчик кнопки 'Пульс рынка'"""
    pulse_text = await get_market_pulse_text()
    await message.answer(pulse_text, parse_mode="HTML")

@dp.message(F.text == "💎 Alpha-Радар")
async def keyboard_gems(message: Message):
    """Обработчик кнопки 'Alpha-Радар'"""
    await gems_cmd(message)

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

@dp.callback_query(lambda c: c.data and c.data == "portfolio")
async def handle_portfolio_button(callback: CallbackQuery):
    """Обработчик кнопки 'Портфель' - только цены, без новостей"""
    await callback.answer()
    await send_portfolio_view()

@dp.callback_query(lambda c: c.data and c.data == "digest")
async def handle_digest_button(callback: CallbackQuery):
    """Обработчик кнопки 'Дайджест дедлайнов' - только новости"""
    await callback.answer()
    await send_news_digest()

@dp.callback_query(lambda c: c.data and c.data == "pulse")
async def handle_pulse_button(callback: CallbackQuery):
    """Обработчик кнопки 'Пульс рынка'"""
    await callback.answer()
    pulse_text = await get_market_pulse_text()
    await callback.message.answer(pulse_text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data and c.data == "cmd_gems")
async def handle_gems_button(callback: CallbackQuery):
    """Обработчик кнопки 'Alpha-Радар' из inline-меню"""
    await callback.answer()
    # Вызываем команду /gems
    await gems_cmd(callback.message)

@dp.callback_query(lambda c: c.data and c.data == "pnl")
async def handle_pnl_button(callback: CallbackQuery):
    """Обработчик кнопки 'Чистый профит / Статистика'"""
    await callback.answer()

    if not gspread_client:
        await callback.message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    await callback.message.answer("📊 Рассчитываю статистику портфеля...")

    loop = asyncio.get_event_loop()

    # Получаем реализованную прибыль
    realized_profit = await loop.run_in_executor(None, get_realized_profit)

    # Получаем спот-позиции и актуальные цены
    spot_positions = await loop.run_in_executor(None, get_spot_positions_with_rows)
    prices_data = await loop.run_in_executor(None, fetch_all_prices)

    # Рассчитываем нереализованный PnL
    unrealized_usd, unrealized_pct = await loop.run_in_executor(
        None, calculate_unrealized_pnl, spot_positions, prices_data
    )

    # Общая прибыль
    total_profit = realized_profit + unrealized_usd

    # Форматирование
    realized_str = f"+${realized_profit:.2f}" if realized_profit >= 0 else f"-${abs(realized_profit):.2f}"
    unrealized_str = f"+${unrealized_usd:.2f}" if unrealized_usd >= 0 else f"-${abs(unrealized_usd):.2f}"
    unrealized_pct_str = f"+{unrealized_pct:.1f}%" if unrealized_pct >= 0 else f"{unrealized_pct:.1f}%"
    total_str = f"+${total_profit:.2f}" if total_profit >= 0 else f"-${abs(total_profit):.2f}"

    # Эмодзи в зависимости от результата
    emoji = "🟢" if total_profit >= 0 else "🔴"

    pnl_text = (
        f"📊 <b>Сводка доходности портфеля:</b>\n\n"
        f"💰 Реализованная чистая прибыль: <b>{realized_str}</b>\n"
        f"📈 Плавающий PnL (открытые позиции): <b>{unrealized_str}</b> ({unrealized_pct_str})\n"
        f"{emoji} <b>Всего заработано: {total_str}</b>"
    )

    await callback.message.answer(pnl_text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data and c.data == "add_asset")
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

@dp.callback_query(lambda c: c.data and c.data == "add_spot")
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

@dp.callback_query(lambda c: c.data and c.data == "add_airdrop")
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

@dp.callback_query(lambda c: c.data and c.data == "manage_assets")
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

    # Получаем зафиксированный профит по этой монете
    ticker_profit = await loop.run_in_executor(None, get_profit_by_ticker, ticker)
    ticker_profit_str = f"+${ticker_profit:.2f}" if ticker_profit >= 0 else f"-${abs(ticker_profit):.2f}"

    card_text = (
        f"💼 <b>{ticker}</b>\n\n"
        f"📊 Количество: <b>{quantity}</b>\n"
        f"💵 Цена входа: <b>${entry_fmt}</b>\n"
        f"💰 Текущая цена: <b>${price_fmt}</b>\n"
        f"🎯 Цель (Тейк): <b>${tp_fmt}</b>\n"
        f"📈 PnL: <b>{pnl:+.1f}%</b>\n"
        f"💎 Зафиксировано по этой монете: <b>{ticker_profit_str}</b>"
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

@dp.callback_query(lambda c: c.data and c.data.startswith("addgem_"))
async def handle_add_gem_to_portfolio(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Добавить в портфель' из карточки гема"""
    await callback.answer()

    if not gspread_client:
        await callback.message.answer("❌ Google Sheets API не настроен. Создайте credentials.json для использования этой функции.")
        return

    try:
        # Парсим callback_data: addgem_TICKER_ENTRY_PRICE_TP2_PRICE
        parts = callback.data.replace("addgem_", "").split("_")
        if len(parts) < 3:
            await callback.message.answer("❌ Ошибка данных")
            return

        ticker = parts[0].upper()
        entry_price = float(parts[1])
        tp_price = float(parts[2])

        # Предзаполняем данные и переходим в состояние добавления
        await state.update_data(
            ticker=ticker,
            entry_price=entry_price,
            tp_price=tp_price,
            from_gem=True
        )

        await callback.message.answer(
            f"💼 <b>Добавление {ticker} в портфель</b>\n\n"
            f"💵 Предложенная цена входа: <b>${entry_price:.6f}</b>\n"
            f"🎯 Предложенная цель: <b>${tp_price:.6f}</b>\n\n"
            f"Введите количество монет для покупки:\n\n"
            f"Пример: <code>1000</code>\n\n"
            f"Отправьте /cancel для отмены.",
            parse_mode="HTML"
        )
        await state.set_state(AddSpotState.waiting_for_spot_data)

    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)}")
        print(f"Ошибка в handle_add_gem_to_portfolio: {e}")

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

    try:
        # Проверяем, добавляем ли из гема
        data = await state.get_data()
        from_gem = data.get("from_gem", False)

        if from_gem:
            # Если добавляем из гема - нужно только количество
            quantity = float(message.text.strip())
            ticker = data.get("ticker")
            entry_price = data.get("entry_price")
            take_profit = data.get("tp_price")
        else:
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
        if data.get("from_gem"):
            await message.answer("❌ Ошибка: количество должно быть числом.\nПример: 1000")
        else:
            await message.answer("❌ Ошибка: количество, вход и тейк должны быть числами.\nПример: NEAR 100 4.5 12.0")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(AddAirdropState.waiting_for_airdrop_data)
async def process_airdrop_data(message: Message, state: FSMContext):
    """Обработка данных для добавления в Радар активностей"""

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
    # Регистрация команд бота в меню Telegram
    commands = [
        BotCommand(command="gems", description="💎 Alpha-Радар (поиск монет вне топ-100)"),
        BotCommand(command="portfolio", description="📊 Спот-портфель"),
        BotCommand(command="pnl", description="💰 Профит и статистика"),
        BotCommand(command="manage", description="✏️ Управление активами"),
        BotCommand(command="digest", description="📰 Дайджест новостей"),
        BotCommand(command="pulse", description="🫀 Рыночный пульс"),
        BotCommand(command="add", description="➕ Добавить актив"),
        BotCommand(command="cancel", description="❌ Отмена")
    ]
    await bot.set_my_commands(commands)
    print("[OK] Bot commands registered")

    # Запуск утреннего дайджеста в 09:00
    scheduler.add_job(send_daily_digest, "cron", hour=9, minute=0)

    # Запуск проверки волатильности каждые 15 минут
    scheduler.add_job(check_volatility_alerts, "interval", minutes=15)

    # Запуск проверки разворота тренда рынка каждые 30 минут
    scheduler.add_job(check_market_trend_reversal, "interval", minutes=30)

    scheduler.start()
    print("[OK] Bot zapuschen. Utrenniy didzhest: 09:00. Proverka volatilnosti: kazhdye 15 minut. Proverka razvorota trenda: kazhdye 30 minut.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
