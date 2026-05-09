# Trade Copilot

Open-source educational auto-trading platform that connects to **Genesis FX (TradeLocker)** accounts and mirrors strategy bot signals based on user-selected risk tolerance.

**Donation supported via [Buy Me a Coffee](https://buymeacoffee.com/dbutler) — no fees, no subscriptions, no claims of returns.**

## Architecture

```
┌─────────────────┐
│ TradingView     │ Strategy bots fire alerts via webhook
│ (Pine Script)   │
└────────┬────────┘
         ▼
┌─────────────────────────┐
│ FastAPI Signal Hub      │ :8000
│ - Webhook receiver      │
│ - Risk engine           │
│ - TradeLocker adapter   │
│ - Discord broadcaster   │
└────────┬────────────────┘
         ▼
┌─────────────────────────┐
│ Genesis FX TradeLocker  │ User accounts execute trades
│ (REST API)              │
└─────────────────────────┘

┌─────────────────────────┐
│ Next.js Dashboard       │ :3000
│ - Bot marketplace       │
│ - Risk slider           │
│ - Live PnL              │
│ - BMC donate button     │
└─────────────────────────┘
```

## Phases

- **Phase 1** — Discord signal service (free + Buy Me a Coffee tip)
- **Phase 2** — Strategy marketplace (Pine Script files, donation-supported)
- **Phase 3** — Auto-execution SaaS via TradeLocker REST API

## Quick Start

```bash
# Backend
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in credentials
uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev  # http://localhost:3000
```

## ⚠️ Important Legal Notice

See [LEGAL.md](LEGAL.md). This software is provided for **educational purposes only**. It is not financial advice. Donations via Buy Me a Coffee are gratuities and do not constitute payment for trading services. Users execute trades at their own risk.

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy + SQLite
- **Frontend**: Next.js 14 + Tailwind + Terminal/TUI theme
- **Broker**: Genesis FX via TradeLocker REST API
- **Signals**: TradingView Pine Script webhooks
- **Donations**: Buy Me a Coffee
- **Notifications**: Discord webhooks
