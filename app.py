import os
import subprocess
import sys
import time
import gradio as gr

# Проверка наличия обязательных переменных окружения
required_vars = ["TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "GOOGLE_SHEETS_ID", "MY_TELEGRAM_ID"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

bot_process = None
bot_status = "⏳ Starting..."

if missing_vars:
    bot_status = f"❌ Missing variables: {', '.join(missing_vars)}"
    print(f"ERROR: {bot_status}", file=sys.stderr)
else:
    try:
        print("✅ All environment variables found. Starting Telegram bot...")
        bot_process = subprocess.Popen(
            [sys.executable, "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        time.sleep(3)  # Даем боту время на запуск

        if bot_process.poll() is None:
            bot_status = "✅ Running"
            print("Bot started successfully")
        else:
            bot_status = "❌ Failed to start"
            print("Bot process terminated", file=sys.stderr)
    except Exception as e:
        bot_status = f"❌ Error: {e}"
        print(f"ERROR starting bot: {e}", file=sys.stderr)

# Создаем простой интерфейс
with gr.Blocks(title="Crypto Scanner Bot") as demo:
    gr.Markdown("# 🚀 Crypto Scanner Bot")
    gr.Markdown(f"## Bot Status: {bot_status}")

    if not missing_vars and bot_status == "✅ Running":
        gr.Markdown("""
        ### Active Features:
        - 📊 Portfolio management with real-time P&L
        - 💎 Alpha-Radar: AI gem discovery (ranks 101-400)
        - 🫀 Market Pulse: Fear & Greed, Funding Rates
        - 🔔 Volatility alerts for ±15% moves
        - 📰 Daily digest at 09:00 UTC
        - 🤖 Automated analysis every 15-30 min

        ### Bot Commands:
        - `/summary` - Portfolio overview
        - `/pulse` - Market pulse with funding rate
        - `/gems` - Alpha-Radar gem scanner
        - `/alerts` - Volatility alert history
        - `/pnl` - Profit & Loss statistics

        **This bot is running in the background via Telegram.**
        """)
    elif missing_vars:
        gr.Markdown(f"""
        ### ⚠️ Configuration Required

        Please add these environment variables in **Settings → Repository secrets**:

        {chr(10).join(f'- `{var}`' for var in missing_vars)}

        Then restart the Space.
        """)
    else:
        gr.Markdown("### ⚠️ Bot failed to start. Check logs for details.")

# Запуск с минимальными параметрами
demo.launch(
    server_name="0.0.0.0",
    server_port=7860,
    show_error=True
)
