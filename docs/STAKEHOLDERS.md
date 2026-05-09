# Stakeholders & Sign-off Process

Last updated: 2026-05-08

## Stakeholders

| Role | Person | Responsibilities | Sign-off rights |
|------|--------|------------------|-----------------|
| Product Owner | Durayveon Butler (`butler135@gmail.com`) | Roadmap, scope, donation messaging, regulatory framing | All P0 changes |
| Technical Lead | Durayveon Butler | Architecture, deployment, security posture | All architectural changes |
| Brand / Community | Jetlag Recovery community | Discord launch coordination, supporter feedback | Phase 1 Discord launch checklist |
| Legal advisor (TBD) | _Open_ | Disclaimer review, regulatory compliance | LEGAL.md edits, donation framing |

> Single-developer project. Sign-off responsibilities consolidated to the owner. As the project grows, additional reviewers will be added per the matrix below.

## Sign-off matrix (RACI-lite)

| Change type | Approver | Reviewer | Notice |
|-------------|----------|----------|--------|
| New feature (frontend or backend) | Product Owner | — | Documented in PR body |
| Schema migration | Tech Lead | — | Migration script + rollback documented |
| Auth / security change | Tech Lead | (Future: external security review) | Code review + security review |
| LEGAL.md edits | Product Owner | Legal advisor when retained | Change log entry required |
| Production deploy | Tech Lead | — | DEPLOY.md runbook followed |
| Public launch / Phase 1 → 2 transition | Product Owner | Brand / Community | Discord announcement + BMC post |
| Strategy live-deploy on real money | Product Owner | Tech Lead + 30-day demo forward-test | Documented Sharpe/DD floor |

## Change management process

1. **Issue raised** — GitHub issue or memory note (`/Users/mac/.claude/projects/-Users-mac/memory/`)
2. **Scoped** — added to `docs/REQUIREMENTS.md` (functional) or `docs/RISK_MATRIX.md` (mitigation) with FR/NFR/R-ID
3. **Designed** — if non-trivial, ADR added under `docs/adr/`
4. **Implemented** — branch with linked test coverage
5. **Reviewed** — at least one independent CI green run + manual dashboard smoke test
6. **Approved** — sign-off captured per matrix above (commit message tag `Approved-by: <role>`)
7. **Released** — `make deploy-backend && make deploy-frontend`, smoke test prod, log entry in `docs/CHANGELOG.md`

## Communication channels

| Channel | Purpose |
|---------|---------|
| GitHub issues | Async tracking |
| Discord `#spirit-clip` (Jetlag) | Phase 1 user feedback |
| Buy Me a Coffee comments | Supporter messages |
| `butler135@gmail.com` | Direct contact, regulatory notices |

## Out of scope

This document does NOT cover:
- Trade execution decisions (the strategy bots own those)
- Donation-tier perk negotiation (BMC handles)
- Individual user TradeLocker disputes (those go to Genesis FX support)
