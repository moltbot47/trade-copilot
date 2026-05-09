# Trade Copilot — Deployment Runbook

End-to-end recipe for getting Trade Copilot from a fresh `git clone` to a
running production deployment. **Backend → Railway, Frontend → Vercel.**

---

## 1. Prerequisites

| Tool          | Version     | Install                                                    |
| ------------- | ----------- | ---------------------------------------------------------- |
| Docker Engine | 24+         | https://docs.docker.com/engine/install/                    |
| Node.js       | 20.x        | `nvm install 20 && nvm use 20`                             |
| Python        | 3.11        | `pyenv install 3.11.9 && pyenv local 3.11.9`               |
| Vercel CLI    | latest      | `npm i -g vercel`                                          |
| Railway CLI   | latest      | `npm i -g @railway/cli`                                    |
| `make`        | any         | preinstalled on macOS / Linux                              |

Optional: `gh` (GitHub CLI) for branch protection setup.

---

## 2. Local development

```bash
git clone <repo> trade-copilot
cd trade-copilot

# Seed env files
cp .env.example backend/.env
cp frontend/.env.local.example frontend/.env.local

# Generate real secrets (see §3) and paste into backend/.env

# Bring the stack up
make dev
```

- Backend: <http://localhost:8000> (`/health` → `{"status":"ok"}`)
- Frontend: <http://localhost:3001>
- Logs: `make logs`
- Tear down: `make dev-down`

Prefer host-mode hot reload? Use `make backend-dev` and `make frontend-dev`
in two terminals.

---

## 3. Generate production secrets

Run these in a clean Python shell — copy the output into Railway env vars,
**never** commit them.

```bash
# SECRET_KEY  (JWT signing)
python -c "import secrets; print(secrets.token_urlsafe(48))"

# ENCRYPTION_KEY  (Fernet, for TradeLocker creds at rest)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Both must be set on the Railway service before first boot, otherwise the
app refuses to start.

---

## 4. Backend deploy — Railway

1. **Login & link**

   ```bash
   railway login
   railway init       # or: railway link <existing-project>
   ```

2. **Set env vars** — in dashboard or via CLI:

   ```bash
   railway variables set \
     SECRET_KEY=...             \
     ENCRYPTION_KEY=...         \
     DATABASE_URL='sqlite:////app/data/trade_copilot.db' \
     ALLOWED_ORIGINS='https://your-frontend.vercel.app'  \
     TRADELOCKER_API_BASE='https://live.tradelocker.com/backend-api' \
     TRADELOCKER_DEMO_API_BASE='https://demo.tradelocker.com/backend-api' \
     BUY_ME_COFFEE_USERNAME=dbutler
   ```

3. **Mount a volume** at `/app/data` (Railway dashboard → Service →
   Volumes → Add). Required for SQLite persistence. Skip if you're using
   the postgres path below.

4. **Deploy**

   ```bash
   make deploy-backend     # equivalent to:  railway up
   ```

5. **Smoke test**

   ```bash
   curl -fsS https://<your-service>.up.railway.app/health
   # → {"status":"ok"}
   ```

### Optional — Postgres instead of SQLite

```bash
railway add postgresql                         # provisions Postgres
railway variables set DATABASE_URL=$DATABASE_URL  # auto-provided
# Drop the /app/data volume if you no longer need it.
```

---

## 5. Frontend deploy — Vercel

1. **Login & link**

   ```bash
   vercel login
   cd frontend && vercel link    # or run from repo root once vercel.json is read
   ```

2. **Set env vars**

   ```bash
   vercel env add NEXT_PUBLIC_API_URL production
   # Paste:  https://<your-railway-service>.up.railway.app

   vercel env add NEXT_PUBLIC_BMC_USERNAME production
   # Paste:  dbutler
   ```

3. **Deploy**

   ```bash
   make deploy-frontend     # equivalent to:  vercel --prod
   ```

4. **Smoke test**

   ```bash
   curl -fsSI https://<your-app>.vercel.app/ | head -1
   # → HTTP/2 200
   ```

5. **Update CORS** — go back to Railway and append the Vercel domain to
   `ALLOWED_ORIGINS`, then redeploy backend.

---

## 6. CI / automated checks

Pushing to `main` (or opening a PR) runs `.github/workflows/ci.yml`:

- **lint** — `ruff check backend/` + `next lint`
- **test** — pytest (+ vitest if frontend tests exist)
- **docker-build** — matrix builds both Dockerfiles via Buildx + GHA cache

Recommended branch protection (GitHub → Settings → Branches → main):

- Require status checks: `Lint`, `Test`, `Docker build (backend)`,
  `Docker build (frontend)`
- Require linear history
- Require PR before merge

---

## 7. Rollback

- **Vercel** — `vercel rollback` or click *Promote* on a previous
  deployment in the dashboard.
- **Railway** — `railway redeploy --service <name>` against a previous
  deploy ID, or use the dashboard's *Rollback* button.

---

## 8. Common pitfalls

| Symptom                                       | Fix                                                                     |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| `cryptography.fernet.InvalidToken` on boot    | `ENCRYPTION_KEY` rotated; old creds can't be decrypted. Regenerate.     |
| 502 on Railway after deploy                   | Service still booting — Railway healthcheck retries `/health` for ~30s. |
| CORS error in browser                         | Add Vercel URL to backend `ALLOWED_ORIGINS`, redeploy backend.          |
| `SECRET_KEY too short` on startup             | Must be ≥32 chars. Use the `secrets.token_urlsafe(48)` recipe in §3.    |
| SQLite resets on every Railway deploy         | Volume not mounted at `/app/data`. See §4 step 3.                       |
| `next lint` fails CI                          | Add eslint config or accept the soft-fail warning (CI keeps going).     |

---

## 9. Health & uptime monitoring

- Backend: `/health` returns 200 + `{"status":"ok"}` once `Wave 1E` is in.
- Hook a free uptime monitor (UptimeRobot / BetterStack) at
  `https://<railway>.up.railway.app/health` with 5-min interval.
- Frontend: Vercel monitors HTTP 2xx on the root domain automatically.
