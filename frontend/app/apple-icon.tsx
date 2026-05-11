/**
 * Apple touch icon — 180x180 PNG.
 *
 * iOS uses this for home-screen bookmarks AND for iMessage link previews.
 * Apple expects an opaque solid-corner image (no transparency); they will
 * mask + round it themselves on iOS 18+.
 */
import { ImageResponse } from "next/og";

export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          background: "#0a0a0a",
          color: "#00ff41",
          fontFamily: "monospace",
        }}
      >
        <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1 }}>▲</div>
        <div
          style={{
            fontSize: 22,
            marginTop: 6,
            letterSpacing: 1.5,
            color: "#e5e5e5",
          }}
        >
          TRADE
        </div>
      </div>
    ),
    { ...size },
  );
}
