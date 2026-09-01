import os
import sys
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# Проверка переменных окружения
required_vars = ["TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "GOOGLE_SHEETS_ID", "MY_TELEGRAM_ID"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

bot_process = None
bot_status = "⏳ Starting..."

if missing_vars:
    bot_status = f"❌ Missing: {', '.join(missing_vars)}"
    print(f"ERROR: {bot_status}", file=sys.stderr)
    print("Configure secrets in Settings → Repository secrets on HF Space", file=sys.stderr)
else:
    print("✅ All environment variables found. Starting bot...")
    try:
        bot_process = subprocess.Popen(
            [sys.executable, "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        time.sleep(2)

        if bot_process.poll() is None:
            bot_status = "✅ Running"
            print("Bot started successfully")
        else:
            bot_status = "❌ Failed to start"
            print("Bot process terminated", file=sys.stderr)
    except Exception as e:
        bot_status = f"❌ Error: {e}"
        print(f"ERROR: {e}", file=sys.stderr)

# Простой HTTP сервер для healthcheck
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Crypto Scanner Bot</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: system-ui; max-width: 800px; margin: 50px auto; padding: 20px; }}
                h1 {{ color: #333; }}
                .status {{ padding: 10px; border-radius: 5px; margin: 20px 0; }}
                .running {{ background: #d4edda; color: #155724; }}
                .error {{ background: #f8d7da; color: #721c24; }}
            </style>
        </head>
        <body>
            <h1>🚀 Crypto Scanner Bot</h1>
            <div class="status {'running' if bot_status.startswith('✅') else 'error'}">
                <strong>Status:</strong> {bot_status}
            </div>
            <h2>Features</h2>
            <ul>
                <li>📊 Portfolio management with P&L tracking</li>
                <li>💎 Alpha-Radar gem discovery</li>
                <li>🫀 Market Pulse analysis</li>
                <li>🔔 Volatility alerts</li>
                <li>📰 Daily digest at 09:00 UTC</li>
            </ul>
            <h2>Bot Commands</h2>
            <ul>
                <li><code>/summary</code> - Portfolio overview</li>
                <li><code>/pulse</code> - Market pulse</li>
                <li><code>/gems</code> - Alpha-Radar scanner</li>
                <li><code>/alerts</code> - Alert history</li>
                <li><code>/pnl</code> - Profit & Loss stats</li>
            </ul>
            {'<p><strong>⚠️ Configure environment variables in Space Settings → Repository secrets</strong></p>' if missing_vars else ''}
        </body>
        </html>
        """
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass  # Отключаем логи запросов

# Запуск сервера на порту 7860
print(f"Starting HTTP server on port 7860...")
print(f"Bot status: {bot_status}")

httpd = HTTPServer(('0.0.0.0', 7860), HealthCheckHandler)
httpd.serve_forever()
