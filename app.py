import os
import sys
import subprocess
import time

# Импорт spaces для HF Space совместимости
try:
    import spaces
    HAS_SPACES = True
except ImportError:
    HAS_SPACES = False

# Проверка переменных окружения
required_vars = ["TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "GOOGLE_SHEETS_ID", "MY_TELEGRAM_ID"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

bot_process = None
bot_status = "⏳ Starting..."

@spaces.GPU(duration=120) if HAS_SPACES else lambda x: x
def start_bot():
    """Функция запуска бота с GPU декоратором для HF Space"""
    global bot_process, bot_status

    if missing_vars:
        bot_status = f"❌ Missing: {', '.join(missing_vars)}"
        print(f"ERROR: {bot_status}", file=sys.stderr)
        print("Configure secrets in Settings → Repository secrets", file=sys.stderr)
        return bot_status

    print("✅ All environment variables found. Starting bot...")
    try:
        bot_process = subprocess.Popen(
            [sys.executable, "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        time.sleep(3)

        if bot_process.poll() is None:
            bot_status = "✅ Running"
            print("Bot started successfully")
        else:
            bot_status = "❌ Failed to start"
            stderr = bot_process.stderr.read() if bot_process.stderr else "No error output"
            print(f"Bot process terminated: {stderr}", file=sys.stderr)
    except Exception as e:
        bot_status = f"❌ Error: {e}"
        print(f"ERROR: {e}", file=sys.stderr)

    return bot_status

# Запускаем бота
start_bot()

# Используем Gradio для совместимости с HF Space
try:
    import gradio as gr

    with gr.Blocks(title="Crypto Scanner Bot") as demo:
        gr.Markdown("# 🚀 Crypto Scanner Bot")
        gr.Markdown(f"## Status: {bot_status}")

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

            **This bot is running in the background and accessible via Telegram.**
            """)
        elif missing_vars:
            gr.Markdown(f"""
            ### ⚠️ Configuration Required

            Add these environment variables in **Settings → Repository secrets**:

            {chr(10).join(f'- `{var}`' for var in missing_vars)}

            Then click **Factory reboot**.
            """)
        else:
            gr.Markdown("### ⚠️ Bot failed to start. Check logs for details.")

    demo.launch(server_name="0.0.0.0", server_port=7860)

except ImportError:
    # Fallback если Gradio не установлен (не должно случиться на HF Space)
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = f"<html><body><h1>Crypto Scanner Bot</h1><p>Status: {bot_status}</p></body></html>"
            self.wfile.write(html.encode())
        def log_message(self, *args): pass

    print("Gradio not found, using HTTP server")
    httpd = HTTPServer(('0.0.0.0', 7860), Handler)
    httpd.serve_forever()
