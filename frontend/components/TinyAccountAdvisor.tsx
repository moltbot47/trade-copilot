"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  AdvisorResponse,
  AdvisorSuggestion,
  RiskAppetite,
} from "@/lib/types";

/**
 * Tiny-account advisor card. Renders after a successful TradeLocker
 * connect on the /connect page. The pitch: "we know your balance, here
 * are the pairs that fit it, ranked from cheapest to subscribe to."
 *
 * Users pick a risk appetite (conservative / balanced / aggressive)
 * which scales the per-pair margin cap and changes the LaT-PFN entry
 * confidence threshold. We DON'T enforce any pair selection — the
 * advisor is purely a suggestion surface, and the existing /bots flow
 * still owns subscription state.
 */
type Props = {
  // Render-once flag: only show on the connect-success path.
  initialAppetite?: RiskAppetite;
};

const APPETITE_OPTIONS: RiskAppetite[] = [
  "conservative",
  "balanced",
  "aggressive",
];

export default function TinyAccountAdvisor({
  initialAppetite = "balanced",
}: Props) {
  const [appetite, setAppetite] = useState<RiskAppetite>(initialAppetite);
  const [data, setData] = useState<AdvisorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getAdvisor(appetite)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error)?.message ?? "advisor error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appetite]);

  return (
    <section
      className="card"
      aria-labelledby="advisor-heading"
      style={{ marginTop: "1rem" }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "0.75rem",
          flexWrap: "wrap",
          marginBottom: "0.75rem",
        }}
      >
        <h2 id="advisor-heading" style={{ margin: 0 }} className="accent">
          {">"} suggested pairs for your account
        </h2>
        <span className="dim" style={{ fontSize: "0.78rem" }}>
          based on balance + appetite · broker confirms at order time
        </span>
      </header>

      {/* Risk-appetite pills */}
      <div
        role="radiogroup"
        aria-label="Risk appetite"
        style={{
          display: "flex",
          gap: "0.4rem",
          marginBottom: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        {APPETITE_OPTIONS.map((opt) => (
          <button
            key={opt}
            type="button"
            role="radio"
            aria-checked={opt === appetite}
            onClick={() => setAppetite(opt)}
            className="btn"
            style={{
              padding: "0.35rem 0.85rem",
              fontSize: "0.85rem",
              borderColor:
                opt === appetite ? "var(--accent)" : "var(--accent-dim)",
              color: opt === appetite ? "var(--accent)" : "var(--dim)",
              fontWeight: opt === appetite ? 700 : 400,
              cursor: "pointer",
            }}
          >
            {opt}
          </button>
        ))}
      </div>

      {/* Preset summary */}
      {data?.preset && (
        <p className="dim" style={{ fontSize: "0.85rem", margin: "0 0 0.5rem" }}>
          {data.preset.description}
        </p>
      )}

      {/* Top-line stats */}
      {data && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
            gap: "0.6rem",
            margin: "0.6rem 0 0.75rem",
            fontSize: "0.85rem",
          }}
        >
          <Stat label="balance" value={`$${data.balance_usd.toFixed(2)}`} />
          <Stat
            label="threshold"
            value={`${data.preset.confidence_threshold.toFixed(2)}σ`}
          />
          <Stat label="target R:R" value={`${data.preset.target_rr.toFixed(1)}:1`} />
          <Stat
            label="concurrent"
            value={`~${data.concurrent_positions_at_min_lot}`}
          />
        </div>
      )}

      {loading && (
        <p className="dim" style={{ fontSize: "0.85rem" }}>
          scanning your broker's instruments…
        </p>
      )}

      {error && (
        <p role="alert" className="danger" style={{ fontSize: "0.85rem" }}>
          advisor failed: {error}
        </p>
      )}

      {data && !loading && data.suggestions.length === 0 && (
        <p className="dim" style={{ fontSize: "0.85rem" }}>
          No fitting pairs at this balance. Either fund the account or pick
          a more aggressive appetite.
        </p>
      )}

      {data && data.suggestions.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          {data.suggestions.slice(0, 8).map((s) => (
            <SuggestionRow key={s.symbol} s={s} />
          ))}
          {data.suggestions.length > 8 && (
            <span className="dim" style={{ fontSize: "0.78rem" }}>
              + {data.suggestions.length - 8} more — switch appetite to see
              different subsets.
            </span>
          )}
        </div>
      )}

      {data?.preset.recommended_bots.length ? (
        <div
          style={{
            marginTop: "0.85rem",
            paddingTop: "0.75rem",
            borderTop: "1px solid var(--accent-dim)",
            fontSize: "0.85rem",
          }}
        >
          <span className="dim">Recommended bots: </span>
          <span className="accent" style={{ fontWeight: 700 }}>
            {data.preset.recommended_bots.join(" · ")}
          </span>
          <a
            href="/bots"
            style={{
              marginLeft: "0.75rem",
              color: "var(--accent)",
              textDecoration: "underline",
            }}
          >
            subscribe →
          </a>
        </div>
      ) : null}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        padding: "0.4rem 0.6rem",
        border: "1px solid var(--accent-dim)",
        borderRadius: 4,
      }}
    >
      <div className="dim" style={{ fontSize: "0.7rem", letterSpacing: 1 }}>
        {label.toUpperCase()}
      </div>
      <div className="accent" style={{ fontWeight: 700 }}>
        {value}
      </div>
    </div>
  );
}

function SuggestionRow({ s }: { s: AdvisorSuggestion }) {
  const status = !s.fits ? "no-fit" : s.warn ? "tight" : "ok";
  const color =
    status === "ok" ? "var(--accent)" : status === "tight" ? "var(--warn)" : "var(--danger)";
  const badge =
    status === "ok" ? "FITS" : status === "tight" ? "TIGHT" : "TOO BIG";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(70px,90px) 70px 1fr",
        gap: "0.6rem",
        alignItems: "center",
        padding: "0.4rem 0.6rem",
        border: `1px solid ${color}`,
        background: status === "ok" ? "transparent" : "rgba(255,255,255,0.02)",
        fontSize: "0.85rem",
      }}
    >
      <span className="accent" style={{ fontWeight: 700 }}>
        {s.symbol}
      </span>
      <span
        style={{
          fontSize: "0.7rem",
          padding: "0.1rem 0.4rem",
          color,
          border: `1px solid ${color}`,
          textAlign: "center",
          letterSpacing: 1,
        }}
      >
        {badge}
      </span>
      <span className="dim">{s.note}</span>
    </div>
  );
}
