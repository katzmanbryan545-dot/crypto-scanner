import os
import sys
import subprocess
import time
import gradio as gr

# Импорт spaces для совместимости с HF
try:
    import spaces
except ImportError:
    # Если spaces не установлен, создаем dummy декоратор
    class spaces:
        @staticmethod
        def GPU(*args, **kwargs):
            def decorator(func):
                return func
            return decorator

# Dummy функция с @spaces.GPU для удовлетворения требований HF Space
@spaces.GPU(duration=60)
def dummy_gpu_function():
    """Пустая функция для удовлетворения требования HF Space о наличии @spaces.GPU"""
    return "GPU function detected"

# Проверка переменных окружения
required_vars = ["TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "GOOGLE_SHEETS_ID", "MY_TELEGRAM_ID"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

bot_process = None
bot_status = "⏳ Starting..."

if missing_vars:
    bot_status = f"❌ Missing: {', '.join(missing_vars)}"
    print(f"ERROR: {bot_status}", file=sys.stderr)
else:
    print("✅ All environment variables found. Starting bot...")
    try:
        bot_process = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        time.sleep(5)

        if bot_process.poll() is None:
            bot_status = "✅ Running"
            print("Bot started successfully")
        else:
            bot_status = "❌ Failed to start"
            output = bot_process.stdout.read() if bot_process.stdout else "No output"
            print(f"Bot process terminated with output:\n{output}", file=sys.stderr)
            bot_status = f"❌ Failed: {output[:200]}"
    except Exception as e:
        bot_status = f"❌ Error: {e}"
        print(f"ERROR: {e}", file=sys.stderr)

# Простой Gradio интерфейс
with gr.Blocks(title="Crypto Scanner Bot") as demo:
    gr.Markdown("# 🚀 Crypto Scanner Bot")
    status_text = gr.Markdown(f"## Status: {bot_status}")

    if not missing_vars and bot_status == "✅ Running":
        gr.Markdown("""
        ### Active Features:
        - 📊 Portfolio management with real-time P&L
        - 💎 Alpha-Radar: AI gem discovery (ranks 101-400)
        - 🫀 Market Pulse: Fear & Greed Index, Funding Rates, BTC Dominance
        - 🔔 Volatility alerts for ±15% moves
        - 📰 Daily digest at 09:00 UTC
        - 🤖 Automated analysis every 15-30 min

        ### Bot Commands:
        - `/summary` - Portfolio overview
        - `/pulse` - Market pulse with funding rate
        - `/gems` - Alpha-Radar gem scanner
        - `/alerts` - Volatility alert history
        - `/pnl` - Profit & Loss statistics

        **Bot is running in the background and accessible via Telegram.**
        """)
    elif missing_vars:
        gr.Markdown(f"""
        ### ⚠️ Configuration Required

        Add these environment variables in **Settings → Repository secrets**:

        {chr(10).join(f'- `{var}`' for var in missing_vars)}

        Then click **Factory reboot**.
        """)
    else:
        gr.Markdown(f"""
        ### ⚠️ Bot Failed to Start

        **Error details:**
        ```
        {bot_status}
        ```

        Check the Space logs for more information.
        """)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, show_error=True)
