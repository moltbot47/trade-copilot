# Security Policy

Trade Copilot trades real money on behalf of its users. We take vulnerability reports seriously and will work in good faith with anyone who reports an issue.

## Supported Versions

Trade Copilot is a single-instance SaaS deployed continuously from `main`. We support exactly the currently deployed revision on Fly.io (backend) and Vercel (frontend). There are no LTS branches.

| Version            | Supported |
|--------------------|-----------|
| `main` (latest deploy on Fly + Vercel) | Yes |
| Staging (`fly.staging.toml`)           | Yes (lower priority) |
| Any prior tag / commit                 | No        |

If you're running a self-hosted fork, you are responsible for tracking upstream commits.

## Reporting a Vulnerability

**Please do not file a public GitHub issue for security problems.**

Preferred channels, in order:

1. **GitHub Security Advisory** — open a private advisory at
   `https://github.com/<owner>/trade-copilot/security/advisories/new` (preferred — keeps the discussion auditable and lets us mint a CVE if needed).
2. **Email** — `security@trade-copilot.dev` (placeholder; update with the production address before publishing this policy).
3. **Discord DM** — to the project owner if the email is unavailable.

Please include:

* A description of the vulnerability and the affected component (file path / endpoint / version if known).
* Steps to reproduce, ideally with a minimal proof-of-concept.
* The impact you believe it has on users / their broker accounts.
* Your name / handle if you'd like to be credited.

### PGP

A PGP public key for `security@trade-copilot.dev` will be published here once it has been generated. For now, please use a GitHub Security Advisory for any report containing exploit details.

```
-----BEGIN PGP PUBLIC KEY BLOCK-----
[placeholder — key not yet generated]
-----END PGP PUBLIC KEY BLOCK-----
```

## Response SLA

* **Triage:** acknowledgement within **48 hours** of receipt.
* **Critical** (account takeover, broker-credential exposure, remote code execution, MFA bypass, signed-webhook bypass): patch and deploy within **30 days**, faster where reasonably possible.
* **High / Medium / Low:** patch within **90 days**.
* **Coordinated disclosure** — we ask reporters to give us the SLA window before publishing details. We will keep you updated weekly during triage and remediation.

If we miss an SLA, you are free to disclose; we will not retaliate.

## Scope

In scope:

* Backend API (`backend/app/**`) — FastAPI app on Fly.io.
* Frontend (`frontend/**`) — Next.js app on Vercel.
* Authentication / session handling (`backend/app/api/auth.py`, `backend/app/core/jwt.py`).
* Multi-factor authentication (`backend/app/api/mfa.py`, `backend/app/auth/mfa.py`).
* TradeLocker integration (`backend/app/api/tradelocker.py`, `backend/app/core/tradelocker_client.py`).
* Inbound webhook signing (`backend/app/api/webhooks.py`, `backend/app/core/webhook_signing.py`).
* Encryption-at-rest (`backend/app/core/crypto.py`).

Out of scope:

* Vulnerabilities in TradeLocker itself (report to TradeLocker).
* Vulnerabilities in Vercel, Fly.io, or upstream Python/Node dependencies (please report upstream and CC us).
* Social engineering against the operator or community.
* Denial of service from a single source already mitigated by rate limits in `backend/app/core/rate_limit.py`.
* Findings that require physical access to the user's device or browser.

## Threat Model

A full STRIDE-based threat model is maintained at [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md). It documents assets, trust boundaries, current mitigations (with file references), and acknowledged residual risks. Please skim it before reporting — there's a chance the issue is already documented and prioritized.

## Hall of Fame

We don't currently run a paid bug bounty, but we are happy to publicly credit researchers who help us fix issues. Once we receive our first report, names (or handles) will be listed here.

| Researcher | Vulnerability | Severity | Date |
|------------|---------------|----------|------|
| _(none yet — be the first!)_ | | | |

Thank you for helping keep Trade Copilot — and our users' broker accounts — safe.
