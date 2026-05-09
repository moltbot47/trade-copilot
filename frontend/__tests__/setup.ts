import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  // happy-dom provides window.localStorage natively. Just clear it.
  if (typeof window !== "undefined" && window.localStorage) {
    window.localStorage.clear();
  }
  // Silence WS client dev-mode debug logs; tests assert on behaviour, not stdout.
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
});
