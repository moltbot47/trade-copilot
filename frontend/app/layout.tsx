import type { Metadata } from "next";
import "./globals.css";
import Layout from "@/components/Layout";
import ErrorBoundary from "@/components/ErrorBoundary";

export const metadata: Metadata = {
  title: "Trade Copilot",
  description:
    "Educational auto-trader for Genesis FX. Donation supported.",
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
