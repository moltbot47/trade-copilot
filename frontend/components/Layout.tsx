"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getUserEmail, clearUserEmail } from "@/lib/api";
import ConnectionStatus from "./ConnectionStatus";
import SessionStatus from "./SessionStatus";

type NavItem = { href: string; label: string; icon: React.ReactNode };

const I = {
  // 20px stroke icons, currentColor for theming.
  bots: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="8" width="18" height="12" rx="2" />
      <path d="M12 3v5M8 14h.01M16 14h.01M9 18h6" />
    </svg>
  ),
  strategy: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 17l5-5 4 4 8-9" />
      <path d="M14 7h6v6" />
    </svg>
  ),
  dashboard: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  ),
  connect: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" />
      <path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" />
    </svg>
  ),
  settings: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  ),
  calculator: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="5" y="3" width="14" height="18" rx="2" />
      <path d="M8 7h8M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01M8 19h8" />
    </svg>
  ),
  donate: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M18 8h1a3 3 0 0 1 0 6h-1" />
      <path d="M3 8h15v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4z" />
      <path d="M6 4v2M10 4v2M14 4v2" />
    </svg>
  ),
  accounts: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  ),
  signOut: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5M21 12H9" />
    </svg>
  ),
  menu: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 6h18M3 12h18M3 18h18" />
    </svg>
  ),
  close: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  ),
};

const NAV: NavItem[] = [
  { href: "/bots", label: "bots", icon: I.bots },
  { href: "/strategy", label: "strategy", icon: I.strategy },
  { href: "/dashboard", label: "dashboard", icon: I.dashboard },
  { href: "/accounts", label: "accounts", icon: I.accounts },
  { href: "/partner", label: "audit", icon: I.dashboard },
  { href: "/connect", label: "connect", icon: I.connect },
  { href: "/settings", label: "settings", icon: I.settings },
  { href: "/calculator", label: "calculator", icon: I.calculator },
  { href: "/donate", label: "donate", icon: I.donate },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [panicPaused, setPanicPaused] = useState<boolean | null>(null);
  const [panicBusy, setPanicBusy] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    setEmail(getUserEmail());
  }, []);

  // Refresh panic status on mount + every 30s while logged in
  useEffect(() => {
    if (!email) return;
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.getPanic();
        if (!cancelled) setPanicPaused(r.bot_paused);
      } catch {
        /* ignore — auth probably missing */
      }
    };
    load();
    const t = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [email]);

  const togglePanic = async () => {
    if (panicPaused === null) return;
    const next = !panicPaused;
    if (next && !confirm(
      "PANIC STOP: this immediately halts all new entries for your account. Open positions stay open but no new trades will fire. Proceed?"
    )) return;
    setPanicBusy(true);
    try {
      const r = await api.setPanic(next);
      setPanicPaused(r.bot_paused);
    } catch (err) {
      alert("Failed to update panic state: " + (err as Error).message);
    } finally {
      setPanicBusy(false);
    }
  };

  // Close mobile nav when navigating to a new path
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  // Lock page scroll while the drawer is open on mobile; close on Esc.
  useEffect(() => {
    if (!navOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [navOpen]);

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

      <header className="app-header">
        <button
          type="button"
          className="nav-toggle"
          aria-expanded={navOpen}
          aria-controls="primary-nav"
          aria-label={navOpen ? "Close menu" : "Open menu"}
          onClick={() => setNavOpen((v) => !v)}
        >
          {navOpen ? I.close : I.menu}
        </button>

        <Link href="/" className="brand-link">
          [trade-copilot]
        </Link>

        {/* Desktop nav — collapses into the mobile drawer at <=768px */}
        <nav
          className="nav-desktop"
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

        <div className="header-user-bar">
          <ConnectionStatus />
          <SessionStatus />
          {email && panicPaused !== null && (
            <button
              onClick={togglePanic}
              disabled={panicBusy}
              title={
                panicPaused
                  ? "All entries currently halted — click to resume"
                  : "Halt all new entries immediately"
              }
              className="panic-btn"
              data-paused={panicPaused ? "true" : "false"}
            >
              {panicBusy ? "…" : panicPaused ? "● paused" : "panic"}
            </button>
          )}
          {email ? (
            <>
              <span className="dim header-user-email-label">user:</span>
              <span className="accent header-user-email">{email}</span>
              <button onClick={signOut} className="sign-out-btn-inline">
                sign out
              </button>
            </>
          ) : (
            <span className="dim">no user</span>
          )}
        </div>
      </header>

      {/* Mobile drawer — slides in from the left with a scrim backdrop. */}
      <div
        className={`drawer-scrim${navOpen ? " is-open" : ""}`}
        onClick={() => setNavOpen(false)}
        aria-hidden={!navOpen}
      />
      <aside
        id="primary-nav"
        className={`drawer${navOpen ? " is-open" : ""}`}
        aria-label="primary navigation"
        aria-hidden={!navOpen}
      >
        <div className="drawer-header">
          <Link href="/" className="brand-link drawer-brand">
            [trade-copilot]
          </Link>
          <button
            type="button"
            onClick={() => setNavOpen(false)}
            aria-label="Close menu"
            className="drawer-close-btn"
          >
            {I.close}
          </button>
        </div>

        <nav className="drawer-nav" aria-label="primary">
          {NAV.map((n) => {
            const active = pathname === n.href || (n.href !== "/" && pathname?.startsWith(n.href));
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={`drawer-link${active ? " is-active" : ""}`}
                tabIndex={navOpen ? 0 : -1}
              >
                <span className="drawer-link-icon">{n.icon}</span>
                <span className="drawer-link-label">{n.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="drawer-footer">
          {email && (
            <div className="drawer-user-block">
              <span className="dim" style={{ fontSize: "0.75rem" }}>signed in as</span>
              <span className="accent drawer-user-email">{email}</span>
            </div>
          )}
          {email && (
            <button onClick={signOut} className="drawer-signout">
              {I.signOut}
              <span>sign out</span>
            </button>
          )}
          {!email && (
            <Link href="/connect" className="drawer-signout" tabIndex={navOpen ? 0 : -1}>
              {I.connect}
              <span>sign in / connect</span>
            </Link>
          )}
        </div>
      </aside>

      <main id="main" tabIndex={-1} className="app-main">
        {children}
      </main>

      <footer className="app-footer">
        <a href="https://github.com/" target="_blank" rel="noopener noreferrer">
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
