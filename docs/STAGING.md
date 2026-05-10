# Staging Environment

Trade Copilot runs a parallel **staging** Fly app
(`trade-copilot-api-staging`) that mirrors prod so we can smoke-test
changes before they reach `trade-copilot-api` (prod). The staging app
auto-deploys on every push to the `staging` branch.

- **Prod**:    https://trade-copilot-api.fly.dev (config: [`fly.toml`](../fly.toml))
- **Staging**: https://trade-copilot-api-staging.fly.dev (config: [`backend/fly.staging.toml`](../backend/fly.staging.toml))

---

## One-time setup (manual — requires `fly auth login`)

These steps create the Fly app, the data volume, and the secrets. Run
them once from your local machine. CI cannot do this because Fly
auth is interactive on first use.

```bash
# 1. Create the staging app under the same Fly org as prod.
fly apps create trade-copilot-api-staging --org personal

# 2. Create the persistent SQLite volume in the same region as prod.
fly volumes create tc_data_staging \
    --size 1 \
    --region iad \
    --app trade-copilot-api-staging

# 3. Set secrets. Mirror prod, but use STAGING-only values where it
#    matters (especially TradeLocker — use demo creds, NOT live).
fly secrets set \
    ENCRYPTION_KEY='<generate-new-fernet-key>' \
    SECRET_KEY='<generate-new-secret>' \
    TRADELOCKER_EMAIL='<demo-account-email>' \
    TRADELOCKER_PASSWORD='<demo-account-password>' \
    TRADELOCKER_SERVER='<demo-server-name>' \
    --app trade-copilot-api-staging

# 4. (Optional) Add a Slack or Discord webhook for deploy pings.
#    Add as a GitHub repo secret named SLACK_WEBHOOK_URL or
#    DISCORD_WEBHOOK_URL — the deploy workflow auto-detects them.

# 5. Verify the FLY_API_TOKEN secret exists in GitHub. Same token
#    used for prod deploys works fine — it's an org-wide token.
gh secret list | grep FLY_API_TOKEN
```

> **Generate fresh Fernet key:**
> `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

---

## Deploying to staging

### Option A — push to the `staging` branch (recommended)

```bash
# From a feature branch
git push origin my-feature-branch:staging
```

This triggers
[`.github/workflows/deploy-staging.yml`](../.github/workflows/deploy-staging.yml)
which runs `flyctl deploy --config backend/fly.staging.toml --strategy immediate`
and then probes `/health` to confirm the new machine is healthy.

### Option B — manual workflow dispatch

```bash
# From any branch, redeploys whatever is currently on `staging`
gh workflow run deploy-staging.yml

# Or from the GitHub UI: Actions tab → Deploy Staging → Run workflow
```

### Option C — local flyctl (emergency)

```bash
# From repo root
flyctl deploy \
    --config backend/fly.staging.toml \
    --dockerfile Dockerfile.backend \
    --strategy immediate \
    .
```

---

## Smoke-testing staging

After a deploy, validate the basics:

```bash
# Liveness
curl -fsS https://trade-copilot-api-staging.fly.dev/health
# Expected: {"status":"ok",...} HTTP 200

# Detailed readiness (probes DB + auxiliary services)
curl -fsS https://trade-copilot-api-staging.fly.dev/health/detail

# CORS check from staging frontend origin
curl -fsS -i \
    -H 'Origin: https://staging-trading.jetlag-recovery.com' \
    https://trade-copilot-api-staging.fly.dev/health

# Live machine status
fly status --app trade-copilot-api-staging
fly logs --app trade-copilot-api-staging
```

For a deeper smoke test, point the staging frontend
(`https://staging-trading.jetlag-recovery.com`) at the staging API and
exercise the auth flow, bot creation, and a tiny demo-account trade.

---

## Promoting staging → prod

The staging environment is the gate to prod. Workflow:

1. Push your feature branch to `staging` and watch the deploy go green.
2. Smoke-test (see above). Let a paper trade or demo bot run for at
   least one full strategy tick if the change touches the runner.
3. Open a PR from `staging` → `main`:
   ```bash
   gh pr create --base main --head staging \
       --title "Promote staging to prod" \
       --body "Smoke test passed: <link to staging deploy run>"
   ```
4. Merge the PR. The
   [`staging-smoke-test`](../.github/workflows/ci.yml) job in CI runs
   one more time on `main` and will fail the build if staging is
   somehow down — this is the final guardrail before prod.
5. Run the prod deploy:
   ```bash
   flyctl deploy --config fly.toml --strategy immediate
   ```
   *(Prod is currently a manual `flyctl deploy`. When that gets
   automated, wire the deploy job to `needs: staging-smoke-test`.)*

---

## Differences between staging and prod

| Setting              | Prod                                 | Staging                                                                |
|----------------------|--------------------------------------|------------------------------------------------------------------------|
| Fly app              | `trade-copilot-api`                  | `trade-copilot-api-staging`                                            |
| `ENVIRONMENT`        | `production`                         | `staging`                                                              |
| `ALLOWED_ORIGINS`    | `https://trading.jetlag-recovery.com`| `https://staging-trading.jetlag-recovery.com,http://localhost:3001`    |
| Volume               | `tc_data` → `/app/data`              | `tc_data_staging` → `/app/data`                                        |
| VM size              | `shared-cpu-1x` / 256 MB             | `shared-cpu-1x` / 256 MB (intentional parity)                          |
| `auto_stop_machines` | `off`                                | `off` (runner must stay warm)                                          |
| Region               | `iad`                                | `iad`                                                                  |
| TradeLocker creds    | LIVE account                         | DEMO account (set via `fly secrets`)                                   |

The compute config is intentionally identical so staging is a real
proxy for prod behavior. Only the data plane and CORS allow-list
differ.

---

## Troubleshooting

**Deploy failed at the build step.** Run the same command locally to
see the docker output: `flyctl deploy --config backend/fly.staging.toml
--dockerfile Dockerfile.backend . --local-only`.

**Deploy succeeded but `/health` is failing.** Check
`fly logs --app trade-copilot-api-staging`. Common causes: missing
secret (the app crash-loops at startup), or the SQLite volume isn't
mounted (check `fly volumes list --app trade-copilot-api-staging`).

**The `staging-smoke-test` job in CI is failing on main.** That's by
design — main builds the prod gate. Either staging is genuinely down
(fix it first) or the staging app hasn't been created yet (run the
one-time setup above). To temporarily bypass, comment out the `if:` on
the job in `ci.yml` — but you've been warned.

**Staging frontend origin doesn't exist yet.** That's fine — the
`ALLOWED_ORIGINS` entry is forward-looking. Staging API will still
accept `http://localhost:3001` so you can hit it from a local frontend
pointed at the staging API URL.
