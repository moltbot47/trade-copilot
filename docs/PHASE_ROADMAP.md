# Trade Copilot — Phase Roadmap

Three phases. Each builds on the previous. Don't skip — Phase 1 is how you find the audience that pays for Phase 3.

## Phase 1 — Discord Signals (live today)

**What it is**: TradingView alerts -> `discord_relay.py` -> Discord channel embed. Read-only signals; no execution.

**Code**: `strategies/discord_relay.py` (FastAPI on port 8001).

**Requires**:
- Discord server + webhook URL
- TradingView Pro account (for webhook alerts)
- The 3 Pine Scripts in `strategies/`

**Launch checklist**:
- [ ] Set `DISCORD_WEBHOOK_URL` in `.env`
- [ ] `python strategies/discord_relay.py` runs cleanly
- [ ] One alert from each Pine script lands in Discord
- [ ] README in Discord channel explains "signals are educational, not financial advice"

**Success metrics**:
- 100 signal posts without an outage
- 50 active members
- 5+ users say "I traded one of these manually and it worked"

---

## Phase 2 — Strategy Marketplace (next)

**What it is**: A web page that lists each Pine Script with description, backtest stats, and a one-click download. Users import into their own TradingView and trade manually.

**Requires**:
- Frontend page at `/strategies` rendering `STRATEGIES.md` + `backtest_results.json`
- Real backtest numbers (not the placeholders) from TradingView Strategy Tester
- A "How to import" video (3 min)
- Buy Me a Coffee button on each strategy page

**Launch checklist**:
- [ ] Real backtest stats per strategy
- [ ] Pine files downloadable as `.pine`
- [ ] Each strategy has a 1-paragraph "what regime it likes" description
- [ ] BMC button per page
- [ ] Disclaimer on every page

**Success metrics**:
- 1,000 unique strategy downloads/month
- 5% donation rate (50 donors/mo)
- 25+ inbound questions/mo from advanced users — that's your Phase 3 demand signal

---

## Phase 3 — Auto-Execution via TradeLocker (the main feature)

**What it is**: Same webhooks, same Pine Scripts, but the backend now routes orders through the TradeLocker REST/WebSocket API to a Genesis FX (or other supported) brokerage account. Risk engine enforces limits, dashboard tracks PnL.

**Requires**:
- Backend FastAPI app at `localhost:8000` (in repo: `backend/`)
- TradeLocker integration (auth, order placement, fill polling, position close)
- Risk engine (see `RISK.md`)
- User dashboard with broker connection, risk settings, trade history
- Discord notifications for fills/stops/errors
- Hardened secrets (vault, not `.env` in prod)
- Audit log + replay capability

**Launch checklist**:
- [ ] `/api/webhooks/tradingview` accepts and routes the documented JSON
- [ ] At least one round-trip trade placed and closed on a TradeLocker demo account
- [ ] Risk engine rejects: missing stop, banned instrument, daily-loss tripped
- [ ] Kill switch in dashboard works
- [ ] `LEGAL.md` + prop-firm warning visible at signup
- [ ] Privacy policy + ToS + risk disclosure
- [ ] Incident runbook + on-call rotation (or auto-pause if backend health fails)

**Success metrics**:
- 100 connected broker accounts
- 30-day retention > 40%
- Zero auto-trade incidents that exceed the user's configured daily-loss cap
- Net Promoter Score > 30

---

## Where You Are Right Now

- Pine Scripts: ready
- Discord relay: ready
- Backend skeleton: present, needs wiring
- Frontend skeleton: present, needs UX
- TradeLocker integration: not started

Ship Phase 1 this week. Phase 2 in 30 days. Phase 3 when 100+ Phase 2 users ask for it.
