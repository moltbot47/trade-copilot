# ADR-0004 — JWT in an HttpOnly cookie for session auth

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

The pre-Wave-1A authentication scheme used an `X-User-Email` request header — the frontend sent the logged-in user's email and the backend trusted it. That is a "trust the header" pattern with zero spoofing resistance: any attacker who knew a target email could impersonate them by hand-crafting a request from any client.

We needed a session mechanism that:
1. Survives a page reload without exposing tokens to JavaScript (no `localStorage`-based bearer tokens leakable to XSS).
2. Works for both browser users and a possible future API client.
3. Doesn't require a heavy session store (no Redis dependency for the MVP).
4. Preserves the MVP's "just give an email" feel — no password barrier on first login.

## Decision

We adopt **HS256 JWT** sessions delivered in an **HttpOnly + SameSite=Lax** cookie named `tc_session`.

- `POST /api/auth/login` with `{email}` (rate-limited to 10/min per IP via slowapi) calls `get_or_create_user` and issues a token signed with `SECRET_KEY`. TTL is 30 days (`SESSION_TTL_DAYS`).
- The cookie is set with `Secure=true` in production (`SESSION_COOKIE_SECURE` defaults to true when `ENVIRONMENT=production`).
- `get_current_user` (in `app/api/users.py`) extracts the token from the cookie first, then from `Authorization: Bearer …` as a fallback for API clients.
- Token claims are minimal: `{sub: email, iat, exp}`. We deliberately do not store roles or permissions in the token — those come from the DB row.
- `POST /api/auth/logout` clears the cookie. It is always 200 (idempotent).

Production-boot guard (`Settings.assert_production_safe`) refuses to start with `SECRET_KEY` or `ENCRYPTION_KEY` left at the placeholder.

## Consequences

**Positive**
- Spoof-resistant: a forged JWT requires the `SECRET_KEY`, which is not present on the client.
- XSS-resistant: HttpOnly cookies cannot be read from JavaScript; a stored XSS in the dashboard cannot exfiltrate the session token.
- CSRF posture: SameSite=Lax blocks cross-site POSTs from third-party origins. The webhook endpoint is the one cross-origin POST surface and is designed for that (no auth required, signed with `bot_secret`).
- Stateless server-side: no session table, no Redis. Cheap.

**Negative**
- Server-side revocation is awkward without a token-blacklist table; for now we accept that logout is "client-side cookie clear only." Wave 2 may add a `token_jti` blacklist if needed.
- Cookie auth requires CORS `allow_credentials=true` and an explicit origin allow-list. The default in production is the deployed frontend origin only.
- Rotating `SECRET_KEY` invalidates every active session.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| `localStorage` bearer tokens | Vulnerable to XSS exfiltration. |
| Server-side sessions (Redis) | Adds a dependency for a feature we don't need at this scale. |
| OAuth via Google / GitHub | Higher friction than the MVP's "just email" promise; will add later if user demand exists. |
| Magic-link emails | Requires an email-sending vendor (Postmark/SES) before we have any users. Defer. |

## Implementation notes

- `app/core/jwt.py` is the single signer/verifier; the rest of the codebase only uses `get_current_user`.
- The fallback `Authorization: Bearer` path lets us write integration tests without juggling cookies.
- The auth router is at `app/api/auth.py` — confirm `app/main.py` mounts it before any deploy. (See `RISK_MATRIX.md` R-04.)
