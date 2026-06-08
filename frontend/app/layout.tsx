import type { Metadata, Viewport } from "next";
import "./globals.css";
import Layout from "@/components/Layout";
import ErrorBoundary from "@/components/ErrorBoundary";

// Resolved at build time. metadataBase is what Next.js uses to build absolute
// URLs for OG / Twitter image tags — without it iMessage and Slack can't
// fetch the preview image and you get a generic gray card.
const SITE_URL =
  process.env.NEXT_PUBLIC_SITE_URL || "https://trading.jetlag-recovery.com";
const TITLE = "Trade Copilot — educational auto-trader";
const DESCRIPTION =
  "Connect a Genesis FX TradeLocker account. Run TradingView Pine Script + LaT-PFN momentum bots. Donation-supported, no subscriptions.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: TITLE,
    template: "%s · Trade Copilot",
  },
  description: DESCRIPTION,
  applicationName: "Trade Copilot",
  authors: [{ name: "Durayveon Butler" }],
  keywords: [
    "Trade Copilot",
    "Genesis FX",
    "TradeLocker",
    "TradingView Pine Script",
    "LaT-PFN",
    "auto-trader",
  ],
  // Open Graph for iMessage / Slack / Facebook / LinkedIn / Discord.
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    url: SITE_URL,
    siteName: "Trade Copilot",
    type: "website",
    locale: "en_US",
    // Next.js auto-includes /opengraph-image (via app/opengraph-image.tsx).
  },
  // Twitter card metadata — separate so Twitter's scraper picks it up.
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: DESCRIPTION,
  },
  robots: {
    index: true,
    follow: true,
  },
  // app/icon.tsx + app/apple-icon.tsx are auto-detected by Next.js; no manual
  // `icons` field needed unless we ship additional sizes.
};

export const viewport: Viewport = {
  themeColor: "#0a0a0a", // matches TUI bg → mobile address bar tinted dark
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover", // let safe-area-inset env() values reach the layout on iOS
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        {/* Buy Me a Coffee widget script */}
        <script
          src="https://cdnjs.buymeacoffee.com/1.0.0/button.prod.min.js"
          async
        ></script>
      </head>
      <body>
        <ErrorBoundary>
          <Layout>{children}</Layout>
        </ErrorBoundary>
      </body>
    </html>
  );
}
