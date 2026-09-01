import subprocess
import gradio as gr

# Фоновый запуск Telegram-бота
subprocess.Popen(["python", "main.py"])

# Минимальный интерфейс-заглушка для Hugging Face
with gr.Blocks() as demo:
    gr.Markdown("# 🚀 Crypto Terminal Bot is Running 24/7")
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

demo.launch(server_name="0.0.0.0", server_port=7860)
