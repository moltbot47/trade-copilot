"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Preview = {
  start_balance: number;
  daily_rate_pct: number;
  days: number;
  end_balance: number;
  multiplier: number;
  milestones: Record<string, number>;
};

const PRESETS = [
  { label: "Conservative", rate: 1.0, sub: "1.0%/day" },
  { label: "Moderate", rate: 2.0, sub: "2.0%/day" },
  { label: "Aggressive", rate: 3.0, sub: "3.0%/day" },
];

function fmtUsd(v: number): string {
  return v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

export default function CalculatorPage() {
  const [startBalance, setStartBalance] = useState<number>(10_000);
  const [dailyRate, setDailyRate] = useState<number>(2.0);
  const [days, setDays] = useState<number>(100);
  const [email, setEmail] = useState<string>("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // Local-fast preview (no backend round-trip)
  const localPreview = useMemo<Preview>(() => {
    const r = dailyRate / 100;
    const end = startBalance * Math.pow(1 + r, days);
    const mk = (d: number) => Math.round(startBalance * Math.pow(1 + r, d) * 100) / 100;
    return {
      start_balance: startBalance,
      daily_rate_pct: dailyRate,
      days,
      end_balance: Math.round(end * 100) / 100,
      multiplier: Math.round((end / startBalance) * 100) / 100,
      milestones: {
        [Math.max(1, Math.floor(days / 10))]: mk(Math.max(1, Math.floor(days / 10))),
        [Math.max(1, Math.floor(days / 4))]: mk(Math.max(1, Math.floor(days / 4))),
        [Math.max(1, Math.floor(days / 2))]: mk(Math.max(1, Math.floor(days / 2))),
        [Math.max(1, Math.floor((days * 3) / 4))]: mk(Math.max(1, Math.floor((days * 3) / 4))),
        [days]: mk(days),
      },
    };
  }, [startBalance, dailyRate, days]);

  // Authoritative preview from backend (debounced)
  useEffect(() => {
    const ctrl = new AbortController();
    const t = setTimeout(async () => {
      try {
        const res = await fetch(`${API_URL}/api/calculator/preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ start_balance: startBalance, daily_rate_pct: dailyRate, days }),
          signal: ctrl.signal,
        });
        if (!res.ok) throw new Error(`backend ${res.status}`);
        setPreview(await res.json());
        setPreviewError(null);
      } catch (e) {
        if ((e as Error).name !== "AbortError") setPreviewError("backend offline · using local math");
      }
    }, 350);
    return () => {
      ctrl.abort();
      clearTimeout(t);
    };
  }, [startBalance, dailyRate, days]);

  const view = preview ?? localPreview;

  const handleDownload = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const res = await fetch(`${API_URL}/api/calculator/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_balance: startBalance,
          daily_rate_pct: dailyRate,
          days,
          requester_label: email || null,
        }),
      });
      if (!res.ok) throw new Error(`backend ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `trade-copilot_compound_${startBalance}_${dailyRate}pct_${days}d.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>{"> "}calculator</div>
        <h1 style={{ marginTop: "0.25rem" }}>compound growth report</h1>
        <p className="dim">
          Project how a starting balance grows under daily compounding. Powered by our{" "}
          <Link href="/strategy" className="accent">extremely low-risk LaT-PFN Momentum strategy</Link>
          {" "}(1–2% daily target). Downloads as a personalized PDF.
        </p>
      </header>

      <div style={{
        display: "grid",
        gridTemplateColumns: "minmax(320px, 1fr) minmax(320px, 1.5fr)",
        gap: "1.5rem",
      }}>
        {/* INPUT FORM */}
        <form
          onSubmit={(e) => { e.preventDefault(); handleDownload(); }}
          className="card"
          style={{ display: "flex", flexDirection: "column", gap: "1rem" }}
        >
          <h2 className="accent" style={{ marginTop: 0 }}>your inputs</h2>

          <div>
            <label htmlFor="bal">starting balance (USD)</label>
            <input
              id="bal"
              type="number"
              min={100}
              max={10_000_000}
              step={100}
              value={startBalance}
              onChange={(e) => setStartBalance(Number(e.target.value))}
              required
            />
          </div>

          <div role="group" aria-labelledby="rate-group-label">
            <span id="rate-group-label" className="dim" style={{
              display: "block",
              fontSize: "0.85rem",
              marginBottom: "0.35rem",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}>
              daily profit target
            </span>
            <div
              style={{ display: "flex", gap: "0.5rem", marginTop: "0.25rem", marginBottom: "0.5rem" }}
              role="radiogroup"
              aria-label="Preset daily profit targets"
            >
              {PRESETS.map((p) => (
                <button
                  key={p.rate}
                  type="button"
                  role="radio"
                  aria-checked={dailyRate === p.rate}
                  onClick={() => setDailyRate(p.rate)}
                  className="btn"
                  style={{
                    flex: 1,
                    background: dailyRate === p.rate ? "var(--accent)" : "transparent",
                    color: dailyRate === p.rate ? "#000" : "var(--text)",
                    border: `1px solid ${dailyRate === p.rate ? "var(--accent)" : "var(--border-strong)"}`,
                    padding: "0.5rem",
                    cursor: "pointer",
                    fontSize: "0.82rem",
                    lineHeight: 1.2,
                  }}
                >
                  <div style={{ fontWeight: 700 }}>{p.label}</div>
                  <div style={{ fontSize: "0.72rem", opacity: 0.85 }}>{p.sub}</div>
                </button>
              ))}
            </div>
            <label htmlFor="custom-rate" className="dim" style={{ fontSize: "0.75rem" }}>
              custom rate (0.1 – 10%/day)
            </label>
            <input
              id="custom-rate"
              type="number"
              min={0.1}
              max={10}
              step={0.1}
              value={dailyRate}
              onChange={(e) => setDailyRate(Number(e.target.value))}
              aria-describedby="custom-rate-hint"
            />
            <div id="custom-rate-hint" className="dim" style={{ fontSize: "0.75rem", marginTop: "0.25rem" }}>
              custom (0.1 – 10%/day)
            </div>
          </div>

          <div>
            <label htmlFor="days">days</label>
            <input
              id="days"
              type="number"
              min={7}
              max={365}
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              required
            />
          </div>

          <div>
            <label htmlFor="email">your name or email <span className="dim">(optional, prints on cover)</span></label>
            <input
              id="email"
              type="text"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              maxLength={120}
            />
          </div>

          <button type="submit" className="btn btn-primary" disabled={downloading}>
            {downloading ? "generating..." : "📄 generate report (PDF)"}
          </button>
          {downloadError && (
            <p
              role="alert"
              aria-live="assertive"
              className="danger"
              style={{ margin: 0, fontSize: "0.85rem" }}
            >
              error: {downloadError}
            </p>
          )}
        </form>

        {/* LIVE PREVIEW */}
        <section className="card" style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
          <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <h2 className="accent" style={{ margin: 0 }}>projected outcome</h2>
            {previewError && <span className="dim" style={{ fontSize: "0.75rem" }}>{previewError}</span>}
          </header>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
            <div>
              <div className="dim" style={{ fontSize: "0.78rem" }}>starting balance</div>
              <div style={{ fontSize: "1.4rem", fontWeight: 600 }}>{fmtUsd(view.start_balance)}</div>
            </div>
            <div>
              <div className="dim" style={{ fontSize: "0.78rem" }}>after {view.days} days</div>
              <div style={{ fontSize: "1.4rem", fontWeight: 600, color: "var(--accent)" }}>
                {fmtUsd(view.end_balance)}
              </div>
              <div className="dim" style={{ fontSize: "0.78rem" }}>{view.multiplier.toFixed(2)}× growth</div>
            </div>
          </div>

          <div>
            <div className="dim" style={{ fontSize: "0.78rem", marginBottom: "0.4rem" }}>milestones</div>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-dim)" }}>
                  <th style={{ padding: "0.3rem 0.4rem", borderBottom: "1px solid var(--border)" }}>day</th>
                  <th style={{ padding: "0.3rem 0.4rem", borderBottom: "1px solid var(--border)" }}>balance</th>
                  <th style={{ padding: "0.3rem 0.4rem", borderBottom: "1px solid var(--border)", textAlign: "right" }}>×</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(view.milestones)
                  .sort(([a], [b]) => Number(a) - Number(b))
                  .map(([d, v]) => (
                    <tr key={d}>
                      <td style={{ padding: "0.3rem 0.4rem", borderBottom: "1px dashed var(--border)" }}>{d}</td>
                      <td style={{ padding: "0.3rem 0.4rem", borderBottom: "1px dashed var(--border)" }}>{fmtUsd(v)}</td>
                      <td style={{ padding: "0.3rem 0.4rem", borderBottom: "1px dashed var(--border)", textAlign: "right" }}>
                        {(v / view.start_balance).toFixed(2)}×
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>

          <p className="dim" style={{ fontSize: "0.78rem", margin: 0 }}>
            Numbers are mathematical projections, not predictions. Actual results depend on
            execution discipline, market conditions, and risk management. Trade Copilot is
            donation-supported software, not investment advice.
          </p>
        </section>
      </div>

      {/* STRATEGY EXPLAINER */}
      <section className="card" style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
        <h2 className="accent" style={{ marginTop: 0 }}>why this report works for our strategy</h2>
        <p style={{ margin: 0 }}>
          The compound projection above assumes you execute the{" "}
          <Link href="/strategy" className="accent">LaT-PFN Momentum strategy</Link>
          {" "}— our extremely low-risk, ML-forecasting bot built around four hard risk controls:
        </p>
        <ul style={{ margin: 0, paddingLeft: "1.25rem", lineHeight: 1.7 }}>
          <li><strong>Confidence-gated entries</strong> — only fires at ≥1.5σ forecast conviction (most signals filtered out)</li>
          <li><strong>ATR-sized hard stops</strong> — every trade has a worst-case loss capped at ~1% of account</li>
          <li><strong>Auto-pause on drawdown</strong> — bot disables itself for 30 min if rolling DD &gt; 10%</li>
          <li><strong>Hedging-mode position isolation</strong> — open trades don't accidentally net against each other</li>
        </ul>
        <div style={{ display: "flex", gap: "0.75rem", marginTop: "0.5rem" }}>
          <Link href="/strategy" className="btn btn-primary" style={{ textDecoration: "none" }}>
            view live strategy →
          </Link>
          <Link href="/connect" className="btn" style={{ textDecoration: "none", border: "1px solid var(--border)" }}>
            connect tradelocker
          </Link>
        </div>
      </section>
    </div>
  );
}
