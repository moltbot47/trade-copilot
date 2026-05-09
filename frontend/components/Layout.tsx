"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getUserEmail, clearUserEmail } from "@/lib/api";
import ConnectionStatus from "./ConnectionStatus";

const NAV = [
  { href: "/bots", label: "bots" },
  { href: "/strategy", label: "strategy" },
  { href: "/calculator", label: "calculator" },
  { href: "/connect", label: "connect" },
  { href: "/dashboard", label: "dashboard" },
  { href: "/donate", label: "donate" },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    setEmail(getUserEmail());
  }, []);

  // Close mobile nav when navigating to a new path
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  const signOut = async () => {
    // Best-effort — even if backend fails, clear client state.
    try {
      await api.logout();
    } catch {
      // ignore
    }
    clearUserEmail();
    if (typeof window !== "undefined") window.location.href = "/";
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <header
        style={{
          borderBottom: "1px solid var(--border)",
          padding: "0.85rem 1.25rem",
          display: "flex",
          alignItems: "center",
          gap: "1.25rem",
          flexWrap: "wrap",
        }}
      >
        <Link
          href="/"
          style={{
            color: "var(--accent)",
            fontWeight: 700,
            letterSpacing: "0.05em",
            textTransform: "uppercase",
          }}
        >
          [trade-copilot]
        </Link>

        <button
          type="button"
          className="nav-toggle"
          aria-expanded={navOpen}
          aria-controls="primary-nav"
          aria-label={navOpen ? "Close menu" : "Open menu"}
          onClick={() => setNavOpen((v) => !v)}
        >
          {navOpen ? "✕" : "☰"}
        </button>

        <nav
          id="primary-nav"
          className={`nav-links${navOpen ? " is-open" : ""}`}
          style={{ display: "flex", gap: "1rem", flex: 1, flexWrap: "wrap" }}
          aria-label="primary"
        >
          {NAV.map((n) => {
            const active = pathname === n.href || (n.href !== "/" && pathname?.startsWith(n.href));
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                style={{
                  color: active ? "var(--accent)" : "var(--text-dim)",
                  fontWeight: active ? 600 : 400,
                  textDecoration: active ? "underline" : "none",
                  textUnderlineOffset: "0.3em",
                }}
              >
                {">"} {n.label}
              </Link>
            );
          })}
        </nav>
        <div
          className="header-user-bar"
          style={{ display: "flex", alignItems: "center", gap: "0.75rem", fontSize: "0.85rem" }}
        >
          <ConnectionStatus />
          {email ? (
            <>
              <span className="dim">user:</span>
              <span className="accent">{email}</span>
              <button
                onClick={signOut}
                style={{
                  background: "transparent",
                  border: "1px solid var(--border-strong)",
                  color: "var(--text-dim)",
                  padding: "0.45rem 0.75rem",
                  cursor: "pointer",
                  minHeight: 44,
                }}
              >
                sign out
              </button>
            </>
          ) : (
            <span className="dim">no user</span>
          )}
        </div>
      </header>

      <main
        id="main"
        tabIndex={-1}
        style={{
          flex: 1,
          padding: "1.5rem 1.25rem",
          maxWidth: 1200,
          width: "100%",
          margin: "0 auto",
          outline: "none",
        }}
      >
        {children}
      </main>

      <footer
        style={{
          borderTop: "1px solid var(--border)",
          padding: "1rem 1.25rem",
          display: "flex",
          gap: "1rem",
          flexWrap: "wrap",
          fontSize: "0.85rem",
          color: "var(--text-dim)",
        }}
      >
        <a
          href="https://github.com/"
          target="_blank"
          rel="noopener noreferrer"
        >
          github
        </a>
        <Link href="/legal">legal</Link>
        <span style={{ marginLeft: "auto" }}>
          made with <span className="bmc-text" style={{ color: "var(--bmc)" }}>coffee</span> — donations welcome
        </span>
      </footer>
    </div>
  );
}
