/**
 * Frontend telemetry — page views + error reporting.
 *
 * - trackPageView(path): POST to /api/metrics/pageview (best-effort, fire-and-forget).
 *   Backend endpoint is currently a stub; calls fail silently.
 * - trackError(err, context): forwards to Sentry if window.Sentry is loaded.
 * - useTelemetry(): hook that fires trackPageView on mount.
 */
"use client";

import { useEffect } from "react";

declare global {
  interface Window {
    Sentry?: {
      captureException?: (err: unknown, ctx?: unknown) => void;
      captureMessage?: (msg: string, ctx?: unknown) => void;
    };
  }
}

const BACKEND_URL =
  (typeof process !== "undefined" && process.env?.NEXT_PUBLIC_BACKEND_URL) ||
  "http://localhost:8000";

export function trackPageView(path: string): void {
  if (typeof window === "undefined") return;
  try {
    const url = `${BACKEND_URL}/api/metrics/pageview`;
    const payload = JSON.stringify({
      path,
      ts: Date.now(),
      ua: navigator.userAgent,
      ref: document.referrer || null,
    });
    if (navigator.sendBeacon) {
      const blob = new Blob([payload], { type: "application/json" });
      navigator.sendBeacon(url, blob);
      return;
    }
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      credentials: "include",
      keepalive: true,
    }).catch(() => {
      /* fire-and-forget */
    });
  } catch {
    /* swallow */
  }
}

export function trackError(err: unknown, context?: Record<string, unknown>): void {
  if (typeof window === "undefined") return;
  try {
    if (window.Sentry?.captureException) {
      window.Sentry.captureException(err, context ? { extra: context } : undefined);
      return;
    }
    // eslint-disable-next-line no-console
    console.error("[telemetry]", err, context);
  } catch {
    /* swallow */
  }
}

/** Hook: fires trackPageView once on mount with the supplied path. */
export function useTelemetry(path?: string): void {
  useEffect(() => {
    const p = path || (typeof window !== "undefined" ? window.location.pathname : "/");
    trackPageView(p);
  }, [path]);
}
