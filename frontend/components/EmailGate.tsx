"use client";

import { useEffect, useState } from "react";
import { api, getUserEmail, setUserEmail } from "@/lib/api";

export default function EmailGate() {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!getUserEmail()) setOpen(true);
  }, []);

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
      setError((err as Error).message || "login failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Identify yourself"
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
        <h2 style={{ margin: 0, marginBottom: "0.5rem" }} className="accent">
          {">"} identify yourself
        </h2>
        <p className="dim" style={{ marginTop: 0, fontSize: "0.85rem" }}>
          We use your email as a lightweight identifier (no password, no
          account). It scopes your bots and TradeLocker connection.
        </p>
        <label htmlFor="email-input">Email</label>
        <input
          id="email-input"
          type="email"
          value={email}
          autoFocus
          onChange={(e) => {
            setEmail(e.target.value);
            setError(null);
          }}
          placeholder="you@example.com"
          disabled={submitting}
        />
        {error && (
          <p className="danger" style={{ marginTop: "0.5rem" }}>
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
