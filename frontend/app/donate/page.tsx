import BMCButton from "@/components/BMCButton";

export default function DonatePage() {
  const username =
    process.env.NEXT_PUBLIC_BMC_USERNAME || "dbutler";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem", maxWidth: 640 }}>
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} keep the lights on
        </div>
        <h1 style={{ marginTop: "0.25rem" }}>donate</h1>
      </header>

      <p>
        Trade Copilot is free, open, and donation supported. There is no
        paywall, no upsell, no MLM, no upsell-disguised-as-mentorship. If
        the project saved you time, made you money, or just made you smile,
        a coffee goes a long way.
      </p>

      <div className="card" style={{ display: "flex", flexDirection: "column", gap: "1rem", alignItems: "flex-start" }}>
        <h2 className="accent" style={{ margin: 0 }}>
          ☕ buy us a coffee
        </h2>
        <p className="dim" style={{ margin: 0 }}>
          Hosted on Buy Me a Coffee. One-time tips and monthly memberships.
        </p>
        <BMCButton label="Open Buy Me a Coffee" />
        <p className="dim" style={{ fontSize: "0.85rem", margin: 0 }}>
          link: https://buymeacoffee.com/{username}
        </p>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          where the money goes
        </h2>
        <ul style={{ margin: 0, paddingLeft: "1.25rem", lineHeight: 1.7 }}>
          <li>VPS / hosting bills</li>
          <li>Strategy R&amp;D — new bots, better backtests</li>
          <li>Open-source dependencies we rely on</li>
          <li>Coffee, obviously</li>
        </ul>
      </div>

      <p className="dim" style={{ fontSize: "0.85rem" }}>
        Thank you. Seriously.
      </p>
    </div>
  );
}
