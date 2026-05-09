# Trade Copilot — Setup Guide

End-to-end setup in ~30 minutes. Educational platform; see `LEGAL.md`.

## 1. Clone

```bash
git clone https://github.com/YOUR_ORG/trade-copilot.git
cd trade-copilot
```

## 2. Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: DATABASE_URL, JWT_SECRET, TRADELOCKER_*, DISCORD_WEBHOOK_URL
uvicorn app.main:app --reload --port 8000
```

Webhook endpoint: `http://localhost:8000/api/webhooks/tradingview`

## 3. Frontend

```bash
cd ../frontend
npm install
cp .env.local.example .env.local
# NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev
```

Dashboard: `http://localhost:3000`

## 4. TradingView Alert Setup

1. Open one of the strategies in `strategies/` (e.g. `orb_breakout.pine`).
2. Click **Pine Editor** -> paste -> **Add to chart**.
3. Right-click chart -> **Add alert**.
4. Condition: select the strategy.
5. **Webhook URL**: `https://YOUR_DOMAIN/api/webhooks/tradingview`
   (use ngrok for local testing.)
6. **Message**: paste the JSON template from the strategy file:

   ```json
   {
     "bot_secret": "orb-breakout",
     "instrument": "{{ticker}}",
     "side": "buy",
     "entry_price": {{close}},
     "stop_loss": {{strategy.position_avg_price}} - 50,
     "take_profit": {{strategy.position_avg_price}} + 75,
     "base_lot_size": 0.10
   }
   ```

7. Save. Repeat per strategy/instrument.

## 5. Discord Webhook (optional, for Phase 1)

1. Discord server -> **Server Settings** -> **Integrations** -> **Webhooks** -> **New Webhook**.
2. Copy URL.
3. Backend `.env`: `DISCORD_WEBHOOK_URL=...`
   Or run the standalone relay:

   ```bash
   python strategies/discord_relay.py
   ```

   This listens on port 8001 and posts to Discord — no DB, no execution.

## 6. Genesis FX TradeLocker Account

1. Create an account: <https://www.genesisfxmarkets.com/> (referral code `DURBUT503`).
2. Fund. Note your TradeLocker login + server.
3. In dashboard -> **Connect Broker** -> select TradeLocker -> paste creds.

## 7. Risk Settings

Dashboard -> **Risk** ->
- Aggression: Conservative / Balanced / Aggressive
- Max daily loss %
- Per-trade risk %
- Allowed instruments

See `RISK.md` for what each lever does.

## 8. Verify First Trade

1. Manually trigger the TradingView alert (right-click candle -> **Add alert** -> **Once per bar** -> save).
2. Backend logs: `tail -f backend/logs/app.log` — look for `webhook received`.
3. Dashboard -> **Trades** — pending order should appear.
4. Broker terminal — confirm fill.

If anything stalls, check `docs/RISK.md` (limits may have blocked the order) and the backend log.
