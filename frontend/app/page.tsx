import Link from "next/link";
import BMCButton from "@/components/BMCButton";

const FEATURES = [
  {
    title: "Strategy marketplace",
    body: "Browse curated, backtested strategy bots. See win rate, profit factor, and risk level before you subscribe.",
  },
  {
    title: "Bring your own broker",
    body: "Connect your Genesis FX TradeLocker account. We never custody funds — you stay in control.",
  },
  {
    title: "Aggression dial",
    body: "Pick a risk level from 1 (conservative) to 10 (aggressive). Change it any time. Pause whenever.",
  },
  {
    title: "Live dashboard",
    body: "Watch balance, equity, open PnL, and a real-time signal log. Built like a terminal you actually want to read.",
  },
  {
    title: "Educational, not advisory",
    body: "Trade Copilot is research and education. Trading futures and FX involves risk of loss. You decide what to run.",
  },
  {
    title: "Donation supported",
    body: "Free to use. If it helps you, drop a coffee. No paywalls, no upsells, no MLM.",
  },
];

const STEPS = [
  {
    n: "01",
    title: "Pick your bots",
    body: "Visit the marketplace and choose strategies that match your style.",
  },
  {
    n: "02",
    title: "Connect TradeLocker",
    body: "Plug in your Genesis FX credentials. We open a session — that's it.",
  },
  {
    n: "03",
    title: "Tune & let it run",
    body: "Set aggression, watch the dashboard, pause whenever you want.",
  },
];

export default function HomePage() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "3rem" }}>
      {/* Hero */}
      <section style={{ padding: "2.5rem 0", borderBottom: "1px solid var(--border)" }}>
        <div className="dim" style={{ fontSize: "0.85rem", letterSpacing: "0.1em" }}>
          {">"} trade-copilot v0.1
        </div>
        <h1
          style={{
            fontSize: "clamp(2rem, 4vw, 3.2rem)",
            margin: "0.5rem 0 1rem",
            color: "var(--accent)",
            lineHeight: 1.1,
          }}
        >
          Educational auto-trader for Genesis FX. Donation supported.
        </h1>
        <p style={{ fontSize: "1.05rem", color: "var(--text)", maxWidth: 700 }}>
          Pick a strategy bot. Connect your TradeLocker account. Pick how
          aggressive you want it. Watch every trade decision land in your
          own Discord channel with full confidence scores. Tip a coffee if
          you like it.
        </p>
        <div style={{ display: "flex", gap: "0.75rem", marginTop: "1.5rem", flexWrap: "wrap" }}>
          <Link href="/bots" className="btn btn-primary">
            Browse bots
          </Link>
          <Link href="/connect" className="btn">
            Connect TradeLocker
          </Link>
          <BMCButton />
        </div>
      </section>

      {/* Features */}
      <section>
        <h2 style={{ marginBottom: "1.25rem" }}>{">"} features</h2>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "1rem",
          }}
        >
          {FEATURES.map((f) => (
            <div className="card" key={f.title}>
              <h3 className="accent" style={{ marginTop: 0 }}>
                {f.title}
              </h3>
              <p style={{ margin: 0, fontSize: "0.92rem" }}>{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section>
        <h2 style={{ marginBottom: "1.25rem" }}>{">"} how it works</h2>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "1rem",
          }}
        >
          {STEPS.map((s) => (
            <div className="card" key={s.n}>
              <div className="dim" style={{ fontSize: "0.85rem" }}>
                step {s.n}
              </div>
              <h3 className="accent" style={{ marginTop: "0.25rem" }}>
                {s.title}
              </h3>
              <p style={{ margin: 0, fontSize: "0.92rem" }}>{s.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Donate CTA */}
      <section
        className="card"
        style={{
          textAlign: "center",
          padding: "2rem",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{">"} like it? buy us a coffee</h2>
        <p className="dim" style={{ maxWidth: 520, margin: "0 auto 1.25rem" }}>
          Trade Copilot is free and donation supported. Every coffee keeps the
          servers running and the strategies improving.
        </p>
        <BMCButton />
      </section>

      <p className="dim" style={{ fontSize: "0.78rem", textAlign: "center" }}>
        See <Link href="/legal">LEGAL</Link> for risk disclosures and terms.
      </p>
    </div>
  );
}
