# Buy Me a Coffee — Donation Setup

Trade Copilot is free and educational. Donations via Buy Me a Coffee keep the lights on without forcing us into subscription/SaaS regulatory grey zones.

## Why Donations, Not Subscriptions

- **Regulatory clarity**: A subscription that sells "trading signals" or "managed trading" can trip securities, CFTC, and state-level licensing rules. Donations for a free educational tool don't.
- **Alignment**: We don't get paid more if you trade more. The platform recommends what's safe, not what generates fees.
- **Lower bar**: No credit card vault, no chargebacks, no auth flows on our side.

## How It Works

1. We host a public Buy Me a Coffee page (`https://buymeacoffee.com/<username>`).
2. The frontend renders a static button + embed widget pointing at that URL.
3. BMC handles checkout, payouts, taxes. We never touch funds.

## Setup

1. Create an account at <https://buymeacoffee.com/>.
2. Pick a username (e.g. `dbutler`).
3. Add to backend `.env`:

   ```env
   BUY_ME_A_COFFEE_USERNAME=dbutler
   ```

4. Frontend `.env.local`:

   ```env
   NEXT_PUBLIC_BMC_USERNAME=dbutler
   ```

## Embedding the Widget

The frontend `<DonateButton />` reads `NEXT_PUBLIC_BMC_USERNAME` and renders:

```tsx
<a
  href={`https://www.buymeacoffee.com/${username}`}
  target="_blank"
  rel="noopener noreferrer"
>
  Buy Me a Coffee
</a>
```

For the floating widget, add to `frontend/app/layout.tsx`:

```html
<script
  data-name="BMC-Widget"
  src="https://cdnjs.buymeacoffee.com/1.0.0/widget.prod.min.js"
  data-id="dbutler"
  data-description="Support Trade Copilot"
  data-message="Thanks for using Trade Copilot."
  data-color="#5F7FFF"
  data-position="Right"
  data-x_margin="18"
  data-y_margin="18"
></script>
```

## Do We Need the BMC API?

No. We use **only** the public profile URL and embed widget. No API keys, no webhooks, no DB sync. If you later want supporter perks (e.g. Discord roles), then look at the BMC Webhook API — but it's outside scope for v1.
