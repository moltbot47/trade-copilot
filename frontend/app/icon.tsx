/**
 * Dynamic favicon — 32x32 PNG rendered by Next.js at request time.
 *
 * Why dynamic instead of a static .ico: lets us keep the brand colors
 * (terminal-green TUI) in sync with the rest of the design without
 * shipping a separate raster asset. Next.js caches the response.
 */
import { ImageResponse } from "next/og";

export const size = { width: 32, height: 32 };
export const contentType = "image/png";

export default function Icon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 22,
          background: "#0a0a0a",
          color: "#00ff41",
          fontWeight: 900,
          fontFamily: "monospace",
          borderRadius: 6,
          border: "1.5px solid #00ff41",
        }}
      >
        ▲
      </div>
    ),
    { ...size },
  );
}
