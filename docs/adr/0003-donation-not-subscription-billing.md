# ADR-0003 — Donation-supported, not subscription billing

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

A platform that places trades on a user's account at user-tunable risk levels lives close to the regulatory line. In the United States, the legal triggers for SEC Investment Adviser registration (RIA) and CFTC Commodity Trading Advisor registration (CTA) are roughly: (a) holding yourself out as offering personalized advice for compensation, (b) managing client funds, or (c) collecting performance-based fees. Charging a fixed monthly subscription — while less risky than performance fees — still creates the "compensation for advice" leg in many state interpretations.

The maintainer is a solo developer building an MVP. The cost of a registration mistake (cease-and-desist, fines, retroactive disgorgement) is asymmetric vs. the upside of monetization at this stage.

## Decision

Trade Copilot is **free to use** and **donation-supported** via Buy Me a Coffee at `buymeacoffee.com/dbutler`.

- No subscription tiers, no in-app payments, no Stripe integration in v0.1.
- No managed accounts — users connect their own broker, so we never custody funds.
- No advice copy — every page reinforces "educational, not advisory" (see `LEGAL.md`, the homepage hero, and the BMC button microcopy).
- Donations are routed entirely off-platform; the BMC link uses `target="_blank" rel="noopener"`.

## Consequences

**Positive**
- Lowest-friction legal posture for an MVP. The product is positioned as research / education, parallel to open-source backtest libraries.
- No PCI obligations; payment data never touches our infrastructure.
- The `LEGAL.md` disclaimers + "trade with your own broker" framing are mutually reinforcing.
- Faster ship — no Stripe integration, webhook idempotency, dunning, or refund flow to build.

**Negative**
- Revenue ceiling is whatever donations yield (typically 0.5–2% of WAU). The product cannot fund a full-time team on donations alone.
- Cannot gate features behind a paywall, so feature-shaped growth metrics are unavailable.
- If the product later wants to monetize, an ADR followup will be needed to document the migration to a compliant billing model (e.g., software license, SaaS tooling fee for non-personalized analytics, formal RIA registration).

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Stripe subscription (e.g., $19/mo) | Triggers RIA/state-adviser scrutiny without registration; legal cost outweighs MVP revenue. |
| Performance fee / profit share | Hard "no" — the most regulated revenue model. |
| One-time license sale | Cleaner legally, but kills retention and ongoing service framing. |
| Affiliate / IB rebate from Genesis FX | Possible later, but mixing affiliate revenue with auto-trade routing creates a conflict-of-interest disclosure burden we don't want pre-launch. |

## Implementation notes

- `BMCButton.tsx` is the canonical donate component.
- `BUY_ME_COFFEE_USERNAME` is a config var (default `dbutler`) so the product can pivot the donation target without a code change.
- Marketing copy has been audited for advice-adjacent language ("recommend," "should," "best") and rewritten to be descriptive instead.
