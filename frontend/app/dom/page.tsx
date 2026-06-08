"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import PriceLadder from "@/components/PriceLadder";
import OrderTicket, { type Brackets } from "@/components/OrderTicket";
import { api } from "@/lib/api";

type Account = Awaited<ReturnType<typeof api.listAccounts>>[number];
type DomState = Awaited<ReturnType<typeof api.domState>>;
type Position = DomState["positions"][number];

const INSTRUMENTS = [
  { symbol: "NAS100", tick: 0.25, label: "Nasdaq 100" },
  { symbol: "SP500", tick: 0.25, label: "S&P 500" },
  { symbol: "US30", tick: 1.0, label: "Dow Jones" },
  { symbol: "XAUUSD", tick: 0.1, label: "Gold" },
  { symbol: "EURUSD", tick: 0.0001, label: "Euro / USD" },
  { symbol: "BTCUSD", tick: 1.0, label: "Bitcoin" },
];

// Above this lot, the buy/sell buttons require an extra confirm tap.
const CONFIRM_LOT_THRESHOLD = 1.0;

// Auto-dismiss the result toast after this many ms so a stale BLOCKED
// message doesn't sit between the stats and BUY/SELL buttons forever.
const TOAST_AUTO_CLEAR_MS = 8_000;

const STORAGE_KEY = "dom.lastConfig.v2";

// Robust BUY/SELL classification — TL has been seen to return side as
// "1"/"2" (string), 1/2 (number), or "buy"/"sell" depending on the
// endpoint shape. Centralize so reverse() / display can't disagree.
function classifySide(raw: unknown): "buy" | "sell" | null {
  if (raw === "1" || raw === 1 || raw === "buy" || raw === "BUY") return "buy";
  if (raw === "2" || raw === 2 || raw === "sell" || raw === "SELL") return "sell";
  return null;
}

export default function DomPage() {
  // ---------- Account state (Phase A delegation) ----------
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [accountsError, setAccountsError] = useState<string | null>(null);

  // ---------- DOM state ----------
  const [instrument, setInstrument] = useState("NAS100");
  const [mid, setMid] = useState(20000);
  const [tickSize, setTickSize] = useState(0.25);
  const [qty, setQty] = useState(0.1);
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [lastResultKind, setLastResultKind] = useState<
    "ok" | "err" | "warn" | null
  >(null);
  const [state, setState] = useState<DomState | null>(null);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [lastTickMs, setLastTickMs] = useState<number | null>(null);
  const [stale, setStale] = useState(false);
  const stateRef = useRef<DomState | null>(null);
  stateRef.current = state;

  // ---------- Load accounts ----------
  useEffect(() => {
    let mounted = true;
    api
      .listAccounts()
      .then((arr) => {
        if (!mounted) return;
        setAccounts(arr);
        if (arr.length > 0) {
          // Prefer last-used from localStorage
          try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
            const last = saved.accountId as number | undefined;
            const found = last ? arr.find((a) => a.id === last) : undefined;
            setAccountId(found ? found.id : arr[0].id);
            if (saved.instrument) setInstrument(saved.instrument);
            if (saved.qty) setQty(saved.qty);
          } catch {
            setAccountId(arr[0].id);
          }
        }
      })
      .catch((e: Error) => {
        if (mounted) setAccountsError(e.message);
      });
    return () => {
      mounted = false;
    };
  }, []);

  // Persist last config
  useEffect(() => {
    if (accountId === null) return;
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ accountId, instrument, qty }),
      );
    } catch {
      /* ignore */
    }
  }, [accountId, instrument, qty]);

  // Sync tick on instrument change
  useEffect(() => {
    const meta = INSTRUMENTS.find((i) => i.symbol === instrument);
    if (meta) setTickSize(meta.tick);
  }, [instrument]);

  // ---------- Poll DOM state (1s for live feel) ----------
  useEffect(() => {
    if (accountId === null) return;
    let cancelled = false;
    const fetchState = async () => {
      const t0 = performance.now();
      try {
        const s = await api.domState(accountId, instrument);
        if (cancelled) return;
        setState(s);
        setLastTickAt(Date.now());
        setLastTickMs(Math.round(performance.now() - t0));
        setStale(false);
      } catch (err) {
        if (!cancelled) {
          setStale(true);
          // Quiet — the heartbeat indicator surfaces this
          console.warn("dom.state error:", err);
        }
      }
    };
    fetchState();
    const t = setInterval(fetchState, 1000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [accountId, instrument]);

  // Mark stale if no tick > 5s
  useEffect(() => {
    if (lastTickAt === null) return;
    const t = setInterval(() => {
      if (Date.now() - lastTickAt > 5000) setStale(true);
    }, 1000);
    return () => clearInterval(t);
  }, [lastTickAt]);

  // Auto-clear the result toast after TOAST_AUTO_CLEAR_MS so a stale
  // BLOCKED/ERROR message doesn't permanently shove the BUY/SELL buttons
  // down the page on mobile.
  useEffect(() => {
    if (lastResult === null) return;
    const t = setTimeout(() => {
      setLastResult(null);
      setLastResultKind(null);
    }, TOAST_AUTO_CLEAR_MS);
    return () => clearTimeout(t);
  }, [lastResult]);

  // ---------- Derived ----------
  const activeAccount = useMemo(
    () => accounts?.find((a) => a.id === accountId) ?? null,
    [accounts, accountId],
  );

  const isViewer = state?.role === "viewer";

  const caps = state?.caps ?? null;
  const capQtyHit =
    caps?.max_lot_per_order != null && qty > caps.max_lot_per_order;

  const dailyLossRoom =
    caps?.max_daily_loss_usd != null
      ? caps.max_daily_loss_usd + (state?.daily_realized_pnl_usd ?? 0)
      : null;
  const dailyLossHit =
    dailyLossRoom !== null && dailyLossRoom <= 0;

  // First open position (used by PriceLadder + Reverse). If side can't be
  // classified, drop the position entirely — better to do nothing than
  // reverse the wrong direction.
  const firstPosition = useMemo(() => {
    if (!state?.positions?.length) return undefined;
    const p = state.positions[0];
    const side = classifySide(p.side);
    if (side === null) return undefined;
    return {
      side,
      qty: Number(p.qty) || 0,
      avgPrice: Number(p.avg_price) || null,
    };
  }, [state]);

  // ---------- Actions ----------
  const submitMarket = useCallback(
    async (side: "buy" | "sell", brackets: Brackets) => {
      if (accountId === null) return;
      if (isViewer) {
        setLastResult("You only have VIEWER access to this account.");
        setLastResultKind("warn");
        return;
      }
      if (qty >= CONFIRM_LOT_THRESHOLD) {
        if (
          !confirm(
            `Confirm ${side.toUpperCase()} ${qty} ${instrument} on ${activeAccount?.label ?? "this account"}?`,
          )
        )
          return;
      }
      setBusy(true);
      setLastResult(null);
      setLastResultKind(null);
      const t0 = performance.now();
      try {
        const r = await api.domOrder({
          account_id: accountId,
          instrument,
          side,
          qty,
          order_type: "market",
          sl: brackets.sl,
          tp: brackets.tp,
        });
        const wall = Math.round(performance.now() - t0);
        if (r.master_status === "filled") {
          setLastResult(
            `OK ${side.toUpperCase()} ${qty} ${instrument} · server ${r.elapsed_ms}ms · total ${wall}ms`,
          );
          setLastResultKind("ok");
        } else if (r.master_status === "blocked") {
          setLastResult(`BLOCKED: ${r.master_error ?? "risk cap"}`);
          setLastResultKind("warn");
        } else {
          setLastResult(
            `${r.master_status.toUpperCase()}: ${r.master_error ?? "no detail"} (${wall}ms)`,
          );
          setLastResultKind("err");
        }
      } catch (err) {
        setLastResult(`ERROR: ${(err as Error).message}`);
        setLastResultKind("err");
      } finally {
        setBusy(false);
      }
    },
    [accountId, instrument, qty, isViewer, activeAccount],
  );

  const flatten = useCallback(async () => {
    if (accountId === null || isViewer) return;
    if (!confirm(`Flatten ALL open positions on ${activeAccount?.label}?`))
      return;
    setBusy(true);
    setLastResult(null);
    setLastResultKind(null);
    try {
      const r = await api.domFlatten(accountId);
      setLastResult(
        `Flattened ${r.closed} in ${r.elapsed_ms}ms` +
          (r.errors.length ? ` · errors: ${r.errors.join("; ")}` : ""),
      );
      setLastResultKind(r.errors.length ? "warn" : "ok");
    } catch (err) {
      setLastResult(`ERROR: ${(err as Error).message}`);
      setLastResultKind("err");
    } finally {
      setBusy(false);
    }
  }, [accountId, isViewer, activeAccount]);

  const reverse = useCallback(async () => {
    if (accountId === null || isViewer) return;
    const pos = stateRef.current?.positions?.[0];
    if (!pos) {
      setLastResult("Nothing to reverse — no open position.");
      setLastResultKind("warn");
      return;
    }
    const currentSide = classifySide(pos.side);
    if (currentSide === null) {
      setLastResult(
        `Refusing to reverse — position side "${pos.side}" not recognized.`,
      );
      setLastResultKind("err");
      return;
    }
    const nextSide: "buy" | "sell" = currentSide === "buy" ? "sell" : "buy";
    const reverseQty = (Number(pos.qty) || qty) * 2;
    // Same client-side cap checks the BUY/SELL buttons enforce. Reverse
    // can be much larger (qty * 2) so the cap check matters more here.
    if (caps?.max_lot_per_order != null && reverseQty > caps.max_lot_per_order) {
      setLastResult(
        `Reverse qty ${reverseQty} exceeds your cap ${caps.max_lot_per_order}.`,
      );
      setLastResultKind("warn");
      return;
    }
    if (dailyLossHit) {
      setLastResult("Daily loss cap reached — reverse blocked.");
      setLastResultKind("warn");
      return;
    }
    if (
      !confirm(
        `Reverse: send ${nextSide.toUpperCase()} ${reverseQty} ${instrument}?`,
      )
    )
      return;
    setBusy(true);
    setLastResult(null);
    setLastResultKind(null);
    try {
      const r = await api.domOrder({
        account_id: accountId,
        instrument,
        side: nextSide,
        qty: reverseQty,
        order_type: "market",
      });
      if (r.master_status === "filled") {
        setLastResult(
          `Reversed → ${nextSide.toUpperCase()} ${reverseQty} (${r.elapsed_ms}ms)`,
        );
        setLastResultKind("ok");
      } else {
        setLastResult(
          `${r.master_status.toUpperCase()}: ${r.master_error ?? "?"}`,
        );
        setLastResultKind("err");
      }
    } catch (err) {
      setLastResult(`ERROR: ${(err as Error).message}`);
      setLastResultKind("err");
    } finally {
      setBusy(false);
    }
  }, [accountId, instrument, qty, isViewer, caps, dailyLossHit]);

  const closeOnePosition = useCallback(
    async (positionId: string) => {
      if (accountId === null || isViewer) return;
      if (!confirm(`Close position ${positionId}?`)) return;
      setBusy(true);
      try {
        const r = await api.domClosePosition(accountId, positionId);
        setLastResult(
          `Closed ${positionId} · realized ${r.realized_pnl_usd.toFixed(2)}`,
        );
        setLastResultKind("ok");
      } catch (err) {
        setLastResult(`ERROR: ${(err as Error).message}`);
        setLastResultKind("err");
      } finally {
        setBusy(false);
      }
    },
    [accountId, isViewer],
  );

  const modifyPositionSlTp = useCallback(
    async (
      positionId: string,
      patch: { stop_loss?: number; take_profit?: number },
    ) => {
      if (accountId === null || isViewer) return;
      setBusy(true);
      try {
        await api.domModifyPosition(accountId, positionId, patch);
        setLastResult(`Modified ${positionId}`);
        setLastResultKind("ok");
      } catch (err) {
        setLastResult(`ERROR: ${(err as Error).message}`);
        setLastResultKind("err");
      } finally {
        setBusy(false);
      }
    },
    [accountId, isViewer],
  );

  const cancelWorkingOrder = useCallback(
    async (orderId: string) => {
      if (accountId === null || isViewer) return;
      if (!confirm(`Cancel working order ${orderId}?`)) return;
      setBusy(true);
      try {
        await api.domCancelOrder(accountId, orderId);
        setLastResult(`Cancelled ${orderId}`);
        setLastResultKind("ok");
      } catch (err) {
        setLastResult(`ERROR: ${(err as Error).message}`);
        setLastResultKind("err");
      } finally {
        setBusy(false);
      }
    },
    [accountId, isViewer],
  );

  // Desktop hotkeys (skipped on mobile via input focus check)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA")
      )
        return;
      if (e.key === "F1") {
        e.preventDefault();
        submitMarket("buy", {});
      } else if (e.key === "F2") {
        e.preventDefault();
        submitMarket("sell", {});
      } else if (e.key === "F3") {
        e.preventDefault();
        flatten();
      } else if (e.key === "+" || (e.key === "=" && e.shiftKey)) {
        e.preventDefault();
        setQty((q) => +(q + 0.01).toFixed(2));
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        setQty((q) => Math.max(0.01, +(q - 0.01).toFixed(2)));
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        reverse();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [submitMarket, flatten, reverse]);

  const handleLadderClick = (price: number, _side: "buy" | "sell") => {
    setMid(price);
    setLastResult(`Mid → ${price}`);
    setLastResultKind("warn");
  };

  // ---------- Empty-state branches ----------
  if (accountsError) {
    return (
      <div className="dom-empty">
        <h1>DOM</h1>
        <p className="danger">Failed to load accounts: {accountsError}</p>
      </div>
    );
  }
  if (accounts === null) {
    return (
      <div className="dom-empty">
        <h1>DOM</h1>
        <p className="dim">Loading accounts…</p>
      </div>
    );
  }
  if (accounts.length === 0) {
    return (
      <div className="dom-empty">
        <h1>DOM — no accounts yet</h1>
        <p className="dim" style={{ maxWidth: 520 }}>
          You need a TradingAccount before you can trade through the DOM.
          1) Go to <a href="/connect">/connect</a> and link your TradeLocker
          broker. 2) Then visit <a href="/accounts">/accounts</a> and click
          &ldquo;register my connected broker as an account.&rdquo; 3) Come back here.
        </p>
      </div>
    );
  }

  const pnlColor =
    state && state.open_pnl > 0
      ? "var(--accent)"
      : state && state.open_pnl < 0
      ? "var(--danger)"
      : "var(--text-dim)";

  return (
    <div className="dom-shell">
      {/* ---------- Account + instrument header ---------- */}
      <div className="dom-acct-bar">
        <div className="dom-acct-bar-left">
          <label className="dim dom-label-text">account</label>
          <select
            value={accountId ?? ""}
            onChange={(e) => setAccountId(Number(e.target.value))}
            className="dom-select dom-select-wide"
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                [{a.role}] {a.label} ({a.env})
              </option>
            ))}
          </select>
        </div>
        <div className="dom-acct-bar-right">
          <label className="dim dom-label-text">instrument</label>
          <select
            value={instrument}
            onChange={(e) => setInstrument(e.target.value)}
            className="dom-select"
          >
            {INSTRUMENTS.map((i) => (
              <option key={i.symbol} value={i.symbol}>
                {i.symbol}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* ---------- Role + caps banner (employees only) ---------- */}
      {activeAccount && activeAccount.role !== "owner" && (
        <div
          className="dom-role-banner"
          data-role={activeAccount.role}
        >
          <strong>{activeAccount.role.toUpperCase()}</strong> access on
          account owned by {activeAccount.owner_email ?? "?"}.
          {caps && (
            <>
              {" "}
              Caps:{" "}
              {caps.max_lot_per_order != null && (
                <span>
                  max lot <strong>{caps.max_lot_per_order}</strong>
                  {" · "}
                </span>
              )}
              {caps.max_daily_loss_usd != null && (
                <span>
                  daily loss cap <strong>${caps.max_daily_loss_usd}</strong>
                  {" · "}
                  used {state?.daily_realized_pnl_usd?.toFixed(2) ?? "0.00"}
                  {" · "}
                </span>
              )}
              {caps.allowed_instruments_csv && (
                <span>
                  symbols <strong>{caps.allowed_instruments_csv}</strong>
                </span>
              )}
            </>
          )}
        </div>
      )}

      {/* ---------- Stats strip ---------- */}
      <div className="dom-stats-grid">
        <Stat label="balance" value={state ? fmtMoney(state.balance) : "—"} />
        <Stat label="available" value={state ? fmtMoney(state.available_funds) : "—"} />
        <Stat
          label="open pnl"
          value={state ? fmtMoney(state.open_pnl) : "—"}
          color={pnlColor}
        />
        <Stat label="positions" value={state ? String(state.positions.length) : "—"} />
        <Stat
          label="connection"
          value={
            <span style={{ color: stale ? "var(--danger)" : "var(--accent)" }}>
              {stale ? "● stale" : "● live"}
              {lastTickMs !== null && (
                <span className="dim" style={{ fontSize: "0.7rem", marginLeft: 4 }}>
                  {lastTickMs}ms
                </span>
              )}
            </span>
          }
        />
      </div>

      {/* ---------- Last result toast ---------- */}
      {lastResult && (
        <div
          className="dom-result-toast"
          data-kind={lastResultKind ?? "ok"}
        >
          {lastResult}
        </div>
      )}

      {/* ---------- Big mobile action buttons ---------- */}
      <div className="dom-mobile-actions">
        <button
          className="dom-big-btn dom-buy"
          onClick={() => submitMarket("buy", {})}
          disabled={busy || isViewer || capQtyHit || dailyLossHit}
        >
          BUY MKT
        </button>
        <button
          className="dom-big-btn dom-sell"
          onClick={() => submitMarket("sell", {})}
          disabled={busy || isViewer || capQtyHit || dailyLossHit}
        >
          SELL MKT
        </button>
      </div>
      <div className="dom-qty-stepper">
        <button
          className="dom-step-btn"
          onClick={() => setQty((q) => Math.max(0.01, +(q - 0.01).toFixed(2)))}
          disabled={busy}
          aria-label="decrease qty"
        >
          −
        </button>
        <div className="dom-qty-display">
          <span className="dim" style={{ fontSize: "0.7rem" }}>qty</span>
          <span>{qty.toFixed(2)}</span>
        </div>
        <button
          className="dom-step-btn"
          onClick={() => setQty((q) => +(q + 0.01).toFixed(2))}
          disabled={busy}
          aria-label="increase qty"
        >
          +
        </button>
      </div>
      {(capQtyHit || dailyLossHit) && (
        <div className="dom-result-toast" data-kind="warn">
          {capQtyHit && `Lot ${qty} exceeds your cap ${caps?.max_lot_per_order}. `}
          {dailyLossHit && `Daily loss cap reached. `}
          Order will be blocked by the server.
        </div>
      )}

      <div className="dom-secondary-actions">
        <button
          className="dom-secondary-btn"
          onClick={reverse}
          disabled={busy || isViewer || !firstPosition}
        >
          REVERSE
        </button>
        <button
          className="dom-secondary-btn dom-flatten"
          onClick={flatten}
          disabled={busy || isViewer || !state || state.positions.length === 0}
        >
          FLATTEN ALL
        </button>
      </div>

      {/* ---------- Positions card ---------- */}
      <section className="dom-section">
        <div className="dom-section-header">
          <h2>Positions</h2>
          <span className="dim">
            {state ? state.positions.length : 0} open
          </span>
        </div>
        {(!state || state.positions.length === 0) && (
          <p className="dim" style={{ padding: "0.85rem 1rem", margin: 0 }}>
            No open positions.
          </p>
        )}
        {state?.positions.map((p, idx) => (
          // Stable key — Math.random would re-mount the row on every 1s
          // poll and destroy any open SL/TP edit form mid-typing. The
          // (id || tradableInstrumentId+side+idx) composite is unique
          // even when broker doesn't supply a position id yet.
          <PositionRow
            key={
              p.id ??
              `${p.tradable_instrument_id}-${p.side}-${p.avg_price}-${idx}`
            }
            position={p}
            disabled={busy || isViewer}
            onClose={() => p.id && closeOnePosition(p.id)}
            onModify={(patch) => p.id && modifyPositionSlTp(p.id, patch)}
          />
        ))}
      </section>

      {/* ---------- Working orders ---------- */}
      {state && state.working_orders.length > 0 && (
        <section className="dom-section">
          <div className="dom-section-header">
            <h2>Working orders</h2>
            <span className="dim">{state.working_orders.length}</span>
          </div>
          {state.working_orders.map((o) => {
            // TL working orders come back as a positional array. Index
            // semantics aren't fully documented but ~idx 3=side, ~idx 4=qty
            // by analogy to positionsConfig. Surface raw context so the
            // employee can tell two orders apart — fall back to id only.
            const raw = (o.raw ?? []) as unknown[] | Record<string, unknown>;
            const arr = Array.isArray(raw) ? raw : [];
            const sideRaw = arr[3];
            const qtyRaw = arr[4];
            const priceRaw = arr[5];
            const sideLabel = classifySide(sideRaw)?.toUpperCase() ?? "?";
            return (
              <div key={o.id} className="dom-working-row">
                <div
                  style={{
                    flex: 1,
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.15rem",
                  }}
                >
                  <div style={{ fontSize: "0.78rem" }}>
                    <span
                      style={{
                        color:
                          sideLabel === "BUY"
                            ? "var(--accent)"
                            : sideLabel === "SELL"
                            ? "var(--danger)"
                            : "var(--text-dim)",
                        fontWeight: 700,
                        marginRight: 6,
                      }}
                    >
                      {sideLabel}
                    </span>
                    {qtyRaw != null && (
                      <span style={{ marginRight: 6 }}>{String(qtyRaw)}</span>
                    )}
                    {priceRaw != null && (
                      <span className="dim">@ {String(priceRaw)}</span>
                    )}
                  </div>
                  <div
                    className="dim"
                    style={{ fontSize: "0.7rem", fontFamily: "monospace" }}
                  >
                    id {o.id}
                  </div>
                </div>
                <button
                  className="dom-secondary-btn"
                  disabled={busy || isViewer}
                  onClick={() => cancelWorkingOrder(o.id)}
                  style={{ padding: "0.5rem 1rem", minHeight: 44 }}
                >
                  cancel
                </button>
              </div>
            );
          })}
        </section>
      )}

      {/* ---------- Desktop-only: ladder + ticket ---------- */}
      <div className="dom-desktop-only" style={{ marginTop: "1.5rem" }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr minmax(280px, 340px)",
            gap: "1rem",
          }}
        >
          <div>
            <PriceLadder
              mid={mid}
              tickSize={tickSize}
              position={firstPosition}
              onClickPrice={handleLadderClick}
            />
            <div
              className="dim"
              style={{
                fontSize: "0.7rem",
                textAlign: "center",
                marginTop: "0.5rem",
              }}
            >
              click a row to recenter — F1 buy · F2 sell · F3 flatten · R reverse · ± qty
            </div>
          </div>
          <OrderTicket
            instrument={instrument}
            qty={qty}
            onQtyChange={setQty}
            onMarketOrder={submitMarket}
            onFlatten={flatten}
            onReverse={reverse}
            busy={busy}
            lastResult={lastResult}
            readOnly={isViewer}
            capQtyHit={capQtyHit}
            dailyLossHit={dailyLossHit}
          />
        </div>
      </div>
    </div>
  );
}

// ---------- PositionRow ----------

function PositionRow({
  position,
  disabled,
  onClose,
  onModify,
}: {
  position: Position;
  disabled: boolean;
  onClose: () => void;
  onModify: (patch: { stop_loss?: number; take_profit?: number }) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [slInput, setSlInput] = useState("");
  const [tpInput, setTpInput] = useState("");

  const side =
    position.side === "1" || position.side === "buy" ? "BUY" : "SELL";
  const sideColor = side === "BUY" ? "var(--accent)" : "var(--danger)";
  const pnl = Number(position.unrealized_pl) || 0;
  const pnlColor =
    pnl > 0 ? "var(--accent)" : pnl < 0 ? "var(--danger)" : "var(--text-dim)";

  const saveModify = () => {
    const sl = slInput ? parseFloat(slInput) : undefined;
    const tp = tpInput ? parseFloat(tpInput) : undefined;
    if (sl === undefined && tp === undefined) {
      setEditing(false);
      return;
    }
    onModify({ stop_loss: sl, take_profit: tp });
    setEditing(false);
    setSlInput("");
    setTpInput("");
  };

  return (
    <div className="dom-position-row">
      <div className="dom-position-top">
        <span style={{ color: sideColor, fontWeight: 700, minWidth: 50 }}>
          {side}
        </span>
        <span style={{ fontWeight: 600 }}>{Number(position.qty)}</span>
        <span className="dim" style={{ fontSize: "0.78rem" }}>
          @ {Number(position.avg_price).toFixed(4)}
        </span>
        <span style={{ marginLeft: "auto", color: pnlColor, fontWeight: 600 }}>
          {pnl >= 0 ? "+" : ""}
          {pnl.toFixed(2)}
        </span>
      </div>
      <div className="dom-position-actions">
        <button
          className="dom-secondary-btn"
          onClick={onClose}
          disabled={disabled}
        >
          close
        </button>
        <button
          className="dom-secondary-btn"
          onClick={() => setEditing((v) => !v)}
          disabled={disabled}
        >
          {editing ? "cancel" : "edit sl/tp"}
        </button>
      </div>
      {editing && (
        <div className="dom-edit-form">
          <input
            type="number"
            inputMode="decimal"
            placeholder="stop loss"
            value={slInput}
            onChange={(e) => setSlInput(e.target.value)}
            className="dom-edit-input"
          />
          <input
            type="number"
            inputMode="decimal"
            placeholder="take profit"
            value={tpInput}
            onChange={(e) => setTpInput(e.target.value)}
            className="dom-edit-input"
          />
          <button
            className="dom-big-btn dom-buy"
            onClick={saveModify}
            disabled={disabled || (!slInput && !tpInput)}
            style={{ minHeight: 44, fontSize: "0.85rem" }}
          >
            save
          </button>
        </div>
      )}
    </div>
  );
}

// ---------- Stat ----------

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: React.ReactNode;
  color?: string;
}) {
  return (
    <div className="dom-stat">
      <div className="dom-stat-label">{label}</div>
      <div className="dom-stat-value" style={{ color: color ?? "var(--text)" }}>
        {value}
      </div>
    </div>
  );
}

function fmtMoney(v: number): string {
  return v.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
