# Deploying Trade Copilot to Koyeb + Neon

End-to-end migration from Fly.io + SQLite to **Koyeb (compute) + Neon
(Postgres)**. Both have free tiers that cover this app.

Total time: ~7 minutes of clicking once you have the secrets handy.

> **Heads up:** Fly is still running the production API. Keep it up
> until Step 7 verifies Koyeb is healthy, then optionally Step 8.

---

## Step 1 — Create Neon project (~3 min, free)

1. Go to https://console.neon.tech/signup
2. Sign in with GitHub.
3. Create project:
   - Name: `trade-copilot`
   - Region: **AWS US East (N. Virginia)** (closest to Koyeb's DC region)
   - Postgres version: **16**
4. On the project dashboard, copy the **Pooled connection** string. It
   looks like:
   ```
   postgres://USER:PASSWORD@ep-xyz-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
   ```
5. Edit the URL before pasting into Koyeb:
   - Change scheme from `postgres://` → `postgresql+psycopg2://` (we
     use the psycopg2 driver). *(Optional — `database.py` will normalize
     it, but explicit is better.)*
   - If you want a dedicated database name, create one in the Neon UI
     (e.g. `trade_copilot`) and replace `/neondb` with it.

Final URL:
```
postgresql+psycopg2://USER:PASSWORD@ep-xyz-pooler.us-east-1.aws.neon.tech/trade_copilot?sslmode=require
```

---

## Step 2 — Sign up for Koyeb (~3 min, free)

1. Go to https://app.koyeb.com/signup
2. Sign in with GitHub (gives Koyeb access to your repos).
3. When prompted, authorize the Koyeb GitHub app for
   `moltbot47/trade-copilot` (you can install it on just this repo).

No card on file required for the free instance.

---

## Step 3 — Create the service

From the Koyeb dashboard:

1. **Apps → Create Service**.
2. **Source**: GitHub → `moltbot47/trade-copilot`, branch `main`.
3. **Build configuration**:
   - Builder: **Dockerfile**
   - Dockerfile path: `Dockerfile.backend`
   - Build context: `.` (repo root)
4. **Instance**: Free (0.1 vCPU, 512 MB RAM).
5. **Region**: **Washington DC (us-east)** — closest to Neon US East.
6. **Ports**: `8000`, protocol `http`, public.
7. **Health check**: HTTP, path `/health`, port `8000`, grace period 30s.
8. **Service name**: `trade-copilot-api`.

Don't click Deploy yet — set env vars first.

---

## Step 4 — Set environment variables (Koyeb UI)

In the service's **Environment variables** panel, add each (mark
secrets as type=Secret so they're encrypted at rest):

| Key | Value | Type |
| --- | ----- | ---- |
| `DATABASE_URL` | the Neon URL from Step 1 (starts `postgresql+psycopg2://`) | secret |
| `SECRET_KEY` | from `/Users/mac/trade-copilot/.deploy-secrets` | secret |
| `ENCRYPTION_KEY` | from `/Users/mac/trade-copilot/.deploy-secrets` | secret |
| `ENVIRONMENT` | `production` | plain |
| `ALLOWED_ORIGINS` | `https://trading.jetlag-recovery.com` | plain |
| `SESSION_COOKIE_SECURE` | `true` | plain |
| `TRADELOCKER_API_BASE` | `https://live.tradelocker.com/backend-api` | plain |
| `TRADELOCKER_DEMO_API_BASE` | `https://demo.tradelocker.com/backend-api` | plain |
| `BUY_ME_COFFEE_USERNAME` | `dbutler` | plain |
| `PORT` | `8000` | plain |

> The `koyeb.yaml` in the repo lists the same vars — keep them in sync
> if you switch to CLI deploys later.

---

## Step 5 — Deploy

Click **Deploy**. First build pulls the multi-stage Docker image from
the GitHub repo and takes ~3-5 minutes.

When the service is **green**:

```bash
# Replace <org> with your Koyeb org slug shown on the service page.
curl https://trade-copilot-api-<org>.koyeb.app/health
# {"status":"ok"}
```

The lifespan hook will:

1. Run `Base.metadata.create_all` against Neon (creates all tables).
2. Apply lightweight migrations (idempotent column adds).
3. Seed the 5 starter bots (idempotent — re-runs are no-ops).

You can confirm via:

```bash
curl https://trade-copilot-api-<org>.koyeb.app/api/bots
# JSON array with 5 bots
```

---

## Step 6 — Custom domain

1. In the Koyeb service settings → **Domains** → **Add domain**:
   `api.trading.jetlag-recovery.com`.
2. Koyeb returns a CNAME target (e.g. `trade-copilot-api-<org>.koyeb.app`).
3. At your DNS provider (currently the same one Fly used), update the
   `api.trading.jetlag-recovery.com` CNAME from the Fly target to the
   Koyeb target.
4. Wait for DNS propagation (usually <5 min, up to 1 hr).
5. Koyeb auto-provisions a Let's Encrypt cert once the CNAME resolves.

---

## Step 7 — Verify production

```bash
curl https://api.trading.jetlag-recovery.com/health
# {"status":"ok"}

curl https://api.trading.jetlag-recovery.com/api/bots
# 5 bots

# Frontend smoke test
open https://trading.jetlag-recovery.com/connect
# - Connect Genesis FX -> reconnect -> should NOT 500 (the c86a6a5 fix)
# - Open a tiny test trade -> sl_moved websocket event fires when SL ratchets
```

If anything fails, the Koyeb dashboard shows live logs under
**Runtime logs**.

---

## Step 8 — Decommission Fly (optional, when ready)

Once Koyeb has handled live traffic for a day or two:

```bash
fly apps destroy trade-copilot-api
```

`fly.toml` stays in the repo as historical reference. The Fly
`/data/trade_copilot.db` SQLite volume is gone forever — pull a backup
first if you want one (`fly ssh console -C 'cat /data/trade_copilot.db' > backup.db`).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `psycopg2.OperationalError: SSL required` | Neon URL missing `sslmode=require` | `database.py` adds it automatically; double-check the env var if it persists |
| Build fails at `pip install psycopg2-binary` | base image missing build tools (shouldn't happen — we use the binary wheel) | rebuild; if it recurs, switch to `psycopg2` and add `libpq-dev` to the runtime image |
| 500 on first request after deploy | seeding hit a race with create_all on cold start | retry; lifespan completes within ~5s |
| "refusing to start in production with placeholder secrets" | `SECRET_KEY` or `ENCRYPTION_KEY` still set to default | set the real values from `.deploy-secrets` |
| WebSocket disconnects every ~60s | Koyeb proxy idle timeout | bump WS heartbeat interval client-side (already 30s) |

---

## Pre-deploy local check

Optional — run before clicking Deploy to confirm the URL works:

```bash
cd /Users/mac/trade-copilot/backend
source venv/bin/activate
DATABASE_URL='postgresql+psycopg2://...neon.tech...?sslmode=require' \
  python scripts/check_postgres_compat.py
```

It connects, runs `Base.metadata.create_all`, applies migrations, and
lists tables. If it succeeds locally, Koyeb will succeed too.
