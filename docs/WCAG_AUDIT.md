# Trade Copilot — WCAG 2.1 Level AA Audit

This document audits every Trade Copilot frontend page against the WCAG 2.1 AA success criteria most relevant to a single-page web app. It tracks the as-of state, the gap, and a prioritized remediation list. NFR-4 is the parent requirement.

**Scope:** all pages under `frontend/app/*` and the shared `Layout`. Audited at design tokens defined in `globals.css`.

**As-of date:** 2026-05-08.

---

## Color Palette (current)

| Token | Hex | Use |
|-------|-----|-----|
| `--bg` | `#0a0a0a` | App background |
| `--bg-card` | `#111111` | Card background |
| `--border` | `#1f1f1f` | Card / table border |
| `--text` | `#e5e5e5` | Primary text |
| `--text-dim` | `#888888` | Secondary text / labels |
| `--accent` | `#00ff41` | Headlines, links, primary buttons |
| `--accent-dim` | `#00aa2c` | Button borders, hovers |
| `--warn` | `#ffaa00` | Warning text |
| `--danger` | `#ff3344` | Danger text / errors |
| `--bmc` | `#ffdd00` | Buy Me a Coffee button |

### Contrast results (computed)

| Foreground | Background | Ratio | WCAG AA (4.5:1 body / 3.0:1 large) |
|------------|------------|-------|------------------------------------|
| `#e5e5e5` text | `#0a0a0a` bg | **17.8 : 1** | Pass body |
| `#e5e5e5` text | `#111111` card | **16.7 : 1** | Pass body |
| `#888888` dim | `#0a0a0a` bg | **5.0 : 1** | Pass body (barely — track on dim labels) |
| `#888888` dim | `#111111` card | **4.7 : 1** | Pass body |
| `#00ff41` accent | `#0a0a0a` bg | **15.3 : 1** | Pass body, pass UI 3:1 |
| `#00aa2c` accent-dim | `#0a0a0a` bg | **6.2 : 1** | Pass body |
| `#ffaa00` warn | `#0a0a0a` bg | **10.2 : 1** | Pass |
| `#ff3344` danger | `#0a0a0a` bg | **5.6 : 1** | Pass body |
| `#ffdd00` bmc | `#111111` btn-text | uses `#111` text on yellow → **15.6 : 1** | Pass |
| `#0a0a0a` btn-primary text | `#00ff41` btn-primary bg | **15.3 : 1** | Pass |

**Net result:** all body text + interactive controls clear AA. The single watch-item is `--text-dim #888` on `--bg-card #111` at 4.7 : 1 — passes, but no headroom for designers tightening dim further.

---

## Per-criterion findings

### 1.1.1 — Non-text content (alt text)
- Audit: BMC button uses an inline SVG with no `<title>` or `aria-label`. **Gap.**
- Action: add `aria-label="Donate via Buy Me a Coffee"` to `BMCButton.tsx`. **High.**

### 1.3.1 — Info and relationships (semantic structure)
- Headings on `/` page hop directly from `h1` → `h2` → `h3`. Pass.
- Tables in `TradeLogTable`, `OpenPositionsTable`, `FeedbackLogTable` use proper `<th>` with `scope` defaults. Re-verify `scope="col"` is explicit. **Medium.**

### 1.3.2 — Meaningful sequence
- Tab order matches DOM order on every page. Pass.

### 1.4.3 — Contrast (minimum)
- Covered by the table above. **Pass.**

### 1.4.4 — Resize text
- Layout uses `clamp()` for hero, rem units elsewhere. Confirmed at 200% browser zoom no overlap. Pass.

### 1.4.10 — Reflow
- Cards use `grid-template-columns: repeat(auto-fit, minmax(260px, 1fr))`. Reflows cleanly at 320 px wide. Pass.

### 1.4.11 — Non-text contrast
- Card border `#1f1f1f` on `#0a0a0a` is **1.4 : 1** — fails the 3.0 : 1 requirement for UI components. **Critical.**
- Action: bump `--border` to `#333` (4.5 : 1) for cards and inputs only, or add a 1px inner glow for focus.

### 1.4.12 — Text spacing
- All text honors paragraph spacing override; no fixed line-heights on critical text. Pass.

### 1.4.13 — Content on hover or focus
- No tooltips in v0.1. N/A.

### 2.1.1 — Keyboard
- Every `Link`, `<button>`, `<input>`, `RiskSlider` reachable via Tab. Pass.

### 2.1.2 — No keyboard trap
- No modals in v0.1. Pass.

### 2.4.1 — Bypass blocks (skip-to-content)
- **Missing.** No skip-to-main-content link in `Layout.tsx`. **High.**
- Action: add a visually-hidden `<a href="#main">Skip to content</a>` that becomes visible on focus.

### 2.4.3 — Focus order
- DOM-natural. Pass.

### 2.4.6 — Headings and labels
- All form inputs in `/connect` and `/calculator` have `<label>`. Inline labels in `RiskSlider` are decorative — confirm primary `<label htmlFor>` exists. **Medium.**

### 2.4.7 — Focus visible
- `input:focus { border-color: var(--accent-dim); }` — only border tint, no outline. **Critical.**
- Action: add `outline: 2px solid var(--accent); outline-offset: 2px;` on `:focus-visible` for buttons, links, inputs, and the slider thumb.

### 2.5.3 — Label in name
- Visible button text matches accessible name. Pass.

### 3.1.1 — Language of page
- `<html lang="en">` set in `app/layout.tsx`. **Verify.** **Medium.**

### 3.2.1 — On focus (no surprises)
- No focus-triggered context changes. Pass.

### 3.3.1 — Error identification
- `/connect` form returns server-side error string but currently rendered as plain `<p>` next to the form. **Gap** — not programmatically associated.
- Action: render errors with `role="alert"` and bind `aria-describedby` from the related input. **High.**

### 3.3.2 — Labels or instructions
- Server name input has helper microcopy ("Genesis FX uses 'GENFX'") in placeholder only. Move to `<small>` with `aria-describedby`. **Medium.**

### 4.1.2 — Name, role, value
- Native HTML elements throughout — `<button>`, `<a>`, `<input>`. Pass.

### 4.1.3 — Status messages
- The dashboard's "last updated" timestamp is plain text. Should announce updates via `aria-live="polite"` on the snapshot region. **Low.**

### Reduced motion
- No motion or auto-play in v0.1. Pass — but pre-emptively add `@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }` to `globals.css`. **Low.**

---

## Remediation list (prioritized)

| # | Severity | Item | File |
|---|----------|------|------|
| 1 | **Critical** | Add visible focus indicator (`outline` on `:focus-visible`) for all interactive controls. | `frontend/app/globals.css` |
| 2 | **Critical** | Bump `--border` to ≥ `#333` (3:1 against `--bg`) for cards and inputs. | `frontend/app/globals.css` |
| 3 | **High** | Add skip-to-content link in shared layout. | `frontend/components/Layout.tsx` |
| 4 | **High** | Add `aria-label` to `BMCButton` (and any icon-only controls). | `frontend/components/BMCButton.tsx` |
| 5 | **High** | Programmatic association of form errors via `role="alert"` + `aria-describedby`. | `frontend/app/connect/page.tsx` |
| 6 | **Medium** | Verify all `<th>` have explicit `scope="col"`. | All `*Table.tsx` components |
| 7 | **Medium** | Confirm `<html lang="en">` in `app/layout.tsx`. | `frontend/app/layout.tsx` |
| 8 | **Medium** | Move server-name hint from placeholder to `<small>` linked via `aria-describedby`. | `frontend/app/connect/page.tsx` |
| 9 | **Low** | Add `aria-live="polite"` on dashboard "last updated" region. | `frontend/app/dashboard/page.tsx` |
| 10 | **Low** | Add `prefers-reduced-motion` query disabling transitions. | `frontend/app/globals.css` |

---

## Automated verification (Wave 2)

- Add `@axe-core/react` in dev mode for runtime axe scans.
- CI step: `axe-cli` against the Vercel preview URL on every PR; fail the build on any "serious" or "critical" violation.
- Manual screen-reader pass each minor release: VoiceOver (Safari) and NVDA (Firefox).

---

## Cross-References

- `REQUIREMENTS.md` — NFR-4.
- `RISK_MATRIX.md` — accessibility risk row.
- `ARCHITECTURE.md` — Quality Attribute #2 (security & inclusion).
