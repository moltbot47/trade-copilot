## Summary

<!-- One or two sentences. What changed and why. -->

## Risk

<!-- One of: low / medium / high. Justify in a sentence. -->

- [ ] **Trading path touched** (signal_router, risk_engine, tradelocker_client, strategies/)
- [ ] **Security-sensitive surface touched** (auth, crypto, jwt, webhook_signing)
- [ ] **Schema migration** (Alembic or lightweight) — describe rollback below

## Test plan

<!-- How did you verify? Tick all that apply. -->

- [ ] `pytest` green locally
- [ ] `vitest` green locally
- [ ] `playwright test` green locally (if frontend UI changed)
- [ ] Manual smoke test on staging (`trade-copilot-api-staging.fly.dev`)
- [ ] Schema migration applied + rollback rehearsed (if applicable)

## Rollback

<!-- How do you undo this if it breaks prod? "Revert commit" is fine for code,
     but schema and config changes need an explicit downgrade path. -->

## Linked issues / context

<!-- Optional: link to issues, ADRs, threat-model entries, or the rolling todo
     in CLAUDE memory. -->
