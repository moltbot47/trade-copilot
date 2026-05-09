"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, getUserEmail, setUserEmail } from "@/lib/api";

function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.kind) {
      case "network":
        return "Server unreachable. Try again.";
      case "auth":
        return "Session expired. Refresh and try again.";
      case "validation":
        return err.message || "Invalid email.";
      case "server":
        return "Server error. Try again in a moment.";
      default:
        return err.message || "Login failed.";
    }
  }
  return (err as Error)?.message || "Login failed.";
}

export default function EmailGate() {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!getUserEmail()) setOpen(true);
  }, []);

  // When the dialog opens, capture the previously focused element and move
  // focus into the dialog. When it closes, restore focus.
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current =
      typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null;
    // Defer to allow the input to mount.
    const t = setTimeout(() => {
      inputRef.current?.focus();
    }, 0);
    return () => {
      clearTimeout(t);
      // Restore focus when this effect tears down (dialog closes/unmounts).
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  // Tab focus trap inside the dialog.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const focusable = root.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = email.trim();
    if (!trimmed || !trimmed.includes("@")) {
      setError("Enter a valid email");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // Hit the backend first — this issues the HttpOnly session cookie.
      const res = await api.login(trimmed);
      setUserEmail(res.email); // localStorage flag so the gate doesn't re-prompt
      setOpen(false);
      // Refresh so all data reloads with the authenticated session.
      if (typeof window !== "undefined") window.location.reload();
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="email-gate-title"
      aria-describedby="email-gate-desc"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.85)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
    >
      <form
        onSubmit={submit}
        className="card"
        style={{ width: "100%", maxWidth: 420 }}
      >
        <h2
          id="email-gate-title"
          style={{ margin: 0, marginBottom: "0.5rem" }}
          className="accent"
        >
          {">"} identify yourself
        </h2>
        <p
          id="email-gate-desc"
          className="dim"
          style={{ marginTop: 0, fontSize: "0.85rem" }}
        >
          We use your email as a lightweight identifier (no password, no
          account). It scopes your bots and TradeLocker connection.
        </p>
        <label htmlFor="email-input">Email</label>
        <input
          id="email-input"
          ref={inputRef}
          type="email"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value);
            setError(null);
          }}
          placeholder="you@example.com"
          disabled={submitting}
          required
          aria-required="true"
          aria-describedby={error ? "email-gate-error" : undefined}
          aria-invalid={!!error}
        />
        {error && (
          <p
            id="email-gate-error"
            role="alert"
            aria-live="assertive"
            className="danger"
            style={{ marginTop: "0.5rem" }}
          >
            {error}
          </p>
        )}
        <div style={{ marginTop: "1rem" }}>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={submitting}
          >
            {submitting ? "signing in..." : "Continue"}
          </button>
        </div>
      </form>
    </div>
  );
}
