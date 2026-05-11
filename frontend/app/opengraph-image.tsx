/**
 * Open Graph image — 1200x630 PNG.
 *
 * Used by iMessage, Facebook, LinkedIn, Slack, and most rich-link previews.
 * Static (compile-time) so every preview shows the same canonical card —
 * dynamic per-route OG images can come later if we want shareable bot pages.
 */
import { ImageResponse } from "next/og";

export const alt = "Trade Copilot — educational auto-trader for Genesis FX";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          background: "#0a0a0a",
          color: "#e5e5e5",
          fontFamily: "monospace",
          padding: "60px 80px",
          justifyContent: "space-between",
        }}
      >
        {/* Top-left brand + status pill */}
        <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
          <div
            style={{
              fontSize: 54,
              color: "#00ff41",
              fontWeight: 900,
              lineHeight: 1,
              letterSpacing: -2,
              fontFamily: "monospace",
            }}
          >
            {">_"}
          </div>
          <div
            style={{
              fontSize: 38,
              fontWeight: 700,
              letterSpacing: 1,
              color: "#e5e5e5",
            }}
          >
            Trade Copilot
          </div>
          <div
            style={{
              marginLeft: "auto",
              display: "flex",
              padding: "8px 18px",
              border: "1.5px solid #00ff41",
              color: "#00ff41",
              fontSize: 20,
              letterSpacing: 2,
              borderRadius: 4,
            }}
          >
            STATUS: LIVE
          </div>
        </div>

        {/* Headline */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div
            style={{
              fontSize: 86,
              fontWeight: 900,
              color: "#00ff41",
              lineHeight: 1.05,
              display: "flex",
            }}
          >
            {">"} educational auto-trader
          </div>
          <div
            style={{
              fontSize: 36,
              color: "#888888",
              lineHeight: 1.2,
              display: "flex",
            }}
          >
            Genesis FX · TradingView signals · LaT-PFN momentum
          </div>
        </div>

        {/* Bottom: URL + tagline */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 24,
            color: "#888888",
          }}
        >
          <span>trading.jetlag-recovery.com</span>
          <span style={{ color: "#ffdd00" }}>
            ☕ supported by Buy Me a Coffee
          </span>
        </div>
      </div>
    ),
    { ...size },
  );
}
