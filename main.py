import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
import asyncio
from openai import OpenAI
import pandas as pd
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from io import StringIO

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Render автоматически передаст эти переменные из панели управления
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", "243120292"))

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
openai_client = OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()

def get_google_sheets_data():
    try:
        csv_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}/export?format=csv"
        response = requests.get(csv_url, timeout=15)
        response.encoding = "utf-8"
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))
    except Exception as e:
        logger.error(f"Ошибка Google Sheets: {e}")
        return None

async def search_crypto_news_direct(query):
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "advanced",
            "max_results": 4,
            "include_answer": False
        }
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=15))
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Сбой Tavily: {e}")
        return None

async def analyze_with_openai(prompt, context=""):
    try:
        full_system_instruction = "Ты профессиональный криптоаналитик, жесткий риск-менеджер и скаут аирдропов. Выдавай отчет в лаконичном, структурированном стиле с фокусом на дедлайны. Обязательно сохраняй и выводи кликабельные ссылки на источники новостей в тексте дайджеста рядом с проектами!"
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": full_system_instruction},
                {"role": "user", "content": f"Контекст новостей:\n{context}\n\nЗадание:\n{prompt}"}
            ],
            temperature=0.2,
            max_tokens=2500
        ))
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка OpenAI API: {e}")
        return "Не удалось сформировать анализ с помощью ИИ."

async def send_daily_news_digest():
    try:
        logger.info("📰 Запуск сбора утреннего дайджеста...")
        df = get_google_sheets_data()
        if df is None or len(df.columns) < 2:
            return

        project_col = df.columns[0]
        status_col = df.columns[1]
        df_filtered = df[df[status_col].notna() & (df[status_col].astype(str).str.strip() != '')]

        if df_filtered.empty:
            return

        all_news = {}
        projects_list = []

        for idx, row in df_filtered.iterrows():
            project = row.get(project_col)
            if pd.isna(project) or str(project).strip() == '':
                continue

            project_name = str(project).strip()
            projects_list.append(project_name)
            
            search_query = f"{project_name} crypto airdrop claim deadline"
            data = await search_crypto_news_direct(search_query)
            
            if not data or not data.get('results'):
                search_query = f"{project_name} crypto news"
                data = await search_crypto_news_direct(search_query)

            if data and data.get('results'):
                all_news[project_name] = data['results']
                logger.info(f"✅ Получено {len(data['results'])} статей для {project_name}")

            await asyncio.sleep(1)

        if not all_news:
            await bot.send_message(MY_TELEGRAM_ID, "📰 Новости не найдены.", parse_mode="HTML")
            return

        news_context = "Сводка новостей по портфелю:\n\n"
        for proj, news_list in all_news.items():
            news_context += f"\n=== {proj.upper()} ===\n"
            for article in news_list:
                news_context += f"Заголовок: {article.get('title', '')}\nURL: {article.get('url', '')}\n"
                if article.get('content'):
                    news_context += f"Текст: {article['content'][:250]}...\n"

        prompt = f"""
        Составь краткий, аналитический утренний дайджест на русском языке для криптоинвестора.
        Используй предоставленный контекст новостей.

        КРИТИЧЕСКИ ВАЖНО:
        Ты — жесткий риск-менеджер. Твоя цель — найти любые упоминания дедлайнов клеймов токенов (claim), даты снэпшотов (snapshot), открытые активности и даты TGE по списку проектов: {', '.join(projects_list)}.

        Правила оформления (используй HTML-теги <b> для жирного текста):
        1. Если обнаружены точные дедлайны, даты или открытые клеймы — вынеси их в самый верх в блок "🚨 СРОЧНО К ВЫПОЛНЕНИЮ". Даты выдели ЖИРНЫМ ШРИФТОМ. Обязательно прикрепи к ним URL-ссылку на источник из контекста!
        2. Для каждого проекта из списка напиши краткий статус (1-2 предложения) на основе новостей. Рядом с названием проекта добавь кликабельную ссылку на источник.
        3. Если конкретных дат и активностей нет, напиши для проекта "⏰ Дедлайны не объявлены, статус стабильный".
        
        Максимум 2500 символов. Четко, емко, без воды.
        """

        digest = await analyze_with_openai(prompt, news_context)
        final_message = f"📰 <b>Ежедневный криптодайджест</b>\n📅 {datetime.now().strftime('%d.%m.%Y')}\n\n{digest}\n\n---\n💡 <i>Автоматический мониторинг портфеля</i>"
        
        await bot.send_message(MY_TELEGRAM_ID, final_message, parse_mode="HTML", disable_web_page_preview=True)
        logger.info("🔥 Дайджест успешно отправлен!")

    except Exception as e:
        logger.error(f"Ошибка дайджеста: {e}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("🚀 <b>Крипто-Сканер v2 запущен на Render!</b>\n\n• /digest - Проверить дедлайны сейчас", parse_mode="HTML")

@dp.message(Command("digest"))
async def cmd_digest(message: Message):
    await message.answer("📰 Запускаю прямое европейское сканирование сети, подождите...")
    await send_daily_news_digest()

async def main():
    try:
        scheduler.add_job(send_daily_news_digest, 'cron', hour=9, minute=0, id='daily_news_digest')
        scheduler.start()
        logger.info("🚀 Бот готов к работе на Render!")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
