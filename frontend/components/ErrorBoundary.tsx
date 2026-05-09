"use client";

import React from "react";

interface Props {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Catches render-time errors below it and shows a fallback. Reports to
 * window.Sentry?.captureException if Sentry has loaded; otherwise just logs.
 *
 * Wrap the app at frontend/app/layout.tsx:
 *   import ErrorBoundary from "@/components/ErrorBoundary";
 *   ...
 *   <ErrorBoundary>
 *     <Layout>{children}</Layout>
 *   </ErrorBoundary>
 */
export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Report to Sentry if loaded; otherwise console.
    const sentry = (typeof window !== "undefined" ? (window as any).Sentry : null) as
      | { captureException?: (e: unknown, ctx?: unknown) => void }
      | null;
    if (sentry?.captureException) {
      try {
        sentry.captureException(error, { extra: { componentStack: info.componentStack } });
      } catch {
        // swallow
      }
    } else if (typeof console !== "undefined") {
      // eslint-disable-next-line no-console
      console.error("ErrorBoundary caught:", error, info);
    }
  }

  reset = () => this.setState({ hasError: false, error: null });

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return <>{this.props.fallback}</>;
      return (
        <div
          role="alert"
          style={{
            padding: 32,
            fontFamily:
              "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
            maxWidth: 640,
            margin: "64px auto",
            borderRadius: 12,
            border: "1px solid #2a2a2a",
            background: "#0e0e0f",
            color: "#e8e8ea",
          }}
        >
          <h2 style={{ margin: 0, marginBottom: 12, fontSize: 20 }}>Something broke.</h2>
          <p style={{ margin: 0, marginBottom: 16, color: "#a8a8ac" }}>
            Refresh the page to retry. If it keeps happening, the issue has been logged.
          </p>
          <button
            type="button"
            onClick={() => {
              this.reset();
              if (typeof window !== "undefined") window.location.reload();
            }}
            style={{
              padding: "8px 16px",
              borderRadius: 8,
              border: "1px solid #3a3a3a",
              background: "#1a1a1c",
              color: "#e8e8ea",
              cursor: "pointer",
            }}
          >
            Refresh
          </button>
        </div>
      );
    }
    return this.props.children as React.ReactElement;
  }
}
