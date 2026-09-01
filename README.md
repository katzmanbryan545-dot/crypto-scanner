---
title: Crypto Terminal Bot
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: gradio
sdk_version: 4.44.0
app_file: app.py
pinned: false
---

# 🚀 Crypto Terminal Bot

24/7 Telegram bot for cryptocurrency portfolio management and market analysis.

## Features

- 📊 **Portfolio Management**: Track positions with real-time P&L
- 💎 **Alpha-Radar**: AI-powered gem discovery (ranks 101-400)
- 🫀 **Market Pulse**: Fear & Greed Index, Funding Rates, BTC Dominance
- 🔔 **Volatility Alerts**: Automatic notifications for ±15% moves
- 📰 **Daily Digest**: Morning market summary at 09:00
- 🤖 **Automated Analysis**: Market trend monitoring every 15-30 min

## Bot Commands

- `/summary` - Quick portfolio overview
- `/pulse` - Market pulse with funding rate
- `/gems` - Alpha-Radar gem scanner
- `/alerts` - Volatility alert history
- `/pnl` - Profit & Loss statistics

## Configuration

Set these environment variables in Space Settings → Repository secrets:

- `TELEGRAM_TOKEN` - Your Telegram bot token
- `OPENROUTER_API_KEY` - OpenRouter API key
- `TAVILY_API_KEY` - Tavily search API key
- `GOOGLE_SHEETS_ID` - Google Sheets ID for portfolio data
- `MY_TELEGRAM_ID` - Your Telegram user ID

## Tech Stack

- **Bot Framework**: aiogram 3.13.1
- **AI**: OpenRouter (DeepSeek Chat)
- **Search**: Tavily API
- **Data**: Google Sheets, CoinGecko API
- **Scheduler**: APScheduler
- **Interface**: Gradio 4.44.0
