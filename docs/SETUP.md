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
6. **Message**: see [Securing your TradingView webhooks](#9-securing-your-tradingview-webhooks)
   below. Two delivery modes are supported.
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

## 9. Securing your TradingView webhooks

TradingView Pine Script can't compute HMAC, so the platform supports
**two** webhook authentication modes. Pick one per alert.

### Why this matters (R-12)

Earlier versions of Trade Copilot identified bots by a public slug
(`"bot_secret": "orb-breakout"`). Slugs appear in the public catalog at
`/api/bots`, so anyone could fire fake signals into the system. R-12 in
`docs/RISK_MATRIX.md` flagged this as a *high* severity defect.

The fix: every bot now has a **per-bot 256-bit HMAC secret**. Inbound
webhooks are authenticated by signing the (timestamp + body) tuple with
that secret using HMAC-SHA256 — same primitive Stripe, GitHub, and
Slack use for their webhooks.

### Mode A — Hardened (PREFERRED)

Use a small relay process that signs requests on the way to Trade
Copilot. The repo ships a starter `strategies/discord_relay.py`; any
similar proxy works.

1. Log in to the dashboard.
2. Fetch your bot's secret:

   ```bash
   curl -H "Authorization: Bearer $TC_TOKEN" \
        https://YOUR_DOMAIN/api/bots/orb-breakout/webhook
   ```

   Response:

   ```json
   {
     "slug": "orb-breakout",
     "webhook_url": "https://YOUR_DOMAIN/api/webhooks/tradingview",
     "secret": "ABC...43-char-token",
     "signature_header_format": "X-Bot-Slug: <slug>; X-Webhook-Timestamp: <unix-seconds>; X-Webhook-Signature: hex(HMAC_SHA256(secret, f'{ts}.{body}'))",
     "max_age_seconds": 300
   }
   ```

3. Configure your relay with `BOT_SLUG`, `BOT_SECRET`, and
   `TC_WEBHOOK_URL`. The relay should:

   - Read the body bytes,
   - Set `ts = int(time.time())`,
   - Compute `sig = HMAC_SHA256(secret, f"{ts}.{body}").hexdigest()`,
   - POST to `TC_WEBHOOK_URL` with headers
     `X-Bot-Slug`, `X-Webhook-Timestamp`, `X-Webhook-Signature`.

4. Point the TradingView **Webhook URL** at your relay (not at
   `/api/webhooks/tradingview` directly).
5. **Message** body (no `bot_secret` field):

   ```json
   {
     "instrument": "{{ticker}}",
     "side": "buy",
     "entry_price": {{close}},
     "stop_loss": {{strategy.position_avg_price}} - 50,
     "take_profit": {{strategy.position_avg_price}} + 75,
     "base_lot_size": 0.10
   }
   ```

6. Replay protection: the server rejects timestamps outside ±300s, so
   stolen requests can't be replayed after the window closes.

### Mode B — Legacy (DEPRECATED)

If you can't run a relay yet, the old body-secret path still works:

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

Each request emits a `webhook.deprecated_body_secret` WARN log on the
server. This path will be removed in a future release — see CHANGELOG.

### Rotating a compromised secret

If you suspect a secret has leaked, rotate it from the dashboard or via
API. The old secret is destroyed immediately:

```bash
curl -X POST -H "Authorization: Bearer $TC_TOKEN" \
     https://YOUR_DOMAIN/api/bots/orb-breakout/webhook/rotate
```

The response carries the new secret exactly once. Update your relay
with the new value and any in-flight requests signed with the old one
will start returning `401 invalid webhook`.
