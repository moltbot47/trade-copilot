import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0a0a0a",
        "bg-card": "#111",
        border: "#1f1f1f",
        text: "#e5e5e5",
        "text-dim": "#888",
        accent: "#00ff41",
        "accent-dim": "#00aa2c",
        warn: "#ffaa00",
        danger: "#ff3344",
        bmc: "#FFDD00",
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', "ui-monospace", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
