import os
import subprocess
import sys
import gradio as gr

# Проверка наличия обязательных переменных окружения
required_vars = ["TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "GOOGLE_SHEETS_ID", "MY_TELEGRAM_ID"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

if missing_vars:
    print(f"❌ ОШИБКА: Отсутствуют переменные окружения: {', '.join(missing_vars)}", file=sys.stderr)
    print("Настройте их в Settings → Repository secrets на Hugging Face Space", file=sys.stderr)
else:
    print("✅ Все переменные окружения найдены. Запускаю Telegram бота...")
    # Фоновый запуск Telegram-бота
    subprocess.Popen([sys.executable, "main.py"],
                     stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE)

# Минимальный интерфейс-заглушка для Hugging Face
with gr.Blocks(title="Crypto Terminal Bot") as demo:
    gr.Markdown("# 🚀 Crypto Terminal Bot is Running 24/7")

    if missing_vars:
        gr.Markdown(f"""
        ## ⚠️ Bot Status: ❌ Configuration Error

        **Missing environment variables:** {', '.join(missing_vars)}

        Please configure them in **Settings → Repository secrets** on Hugging Face Space.
        """)
    else:
        gr.Markdown("""
        ## Bot Status: ✅ Active

        Telegram Bot: [@cryptoscannerfeedbot](https://t.me/cryptoscannerfeedbot)

        ### Features:
        - 📊 Portfolio management with ROI & Win Rate analytics
        - 💎 Alpha-Radar: AI-powered gem discovery
        - 🫀 Market Pulse with Funding Rate integration
        - 🔔 Volatility alerts with history
        - 📰 Daily digest at 09:00
        - 🤖 Automated market analysis every 15/30 minutes

        ### Commands:
        - `/summary` - Quick portfolio overview
        - `/pulse` - Market pulse with funding rate
        - `/gems` - Alpha-Radar gem scanner
        - `/alerts` - Volatility alert history
        - `/pnl` - Profit & Loss statistics

        **Note:** This interface is for Hugging Face Space deployment only.
        The actual bot is running in the background and accessible via Telegram.
        """)

# Запуск с правильными параметрами для Hugging Face Space
demo.launch(
    server_name="0.0.0.0",
    server_port=7860,
    show_error=True,
    quiet=False
)
