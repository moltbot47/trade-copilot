"""Interactive Discord bot for Trade Copilot.

Runs as an asyncio task inside the FastAPI lifespan. Reads the DB
directly (no HTTP round-trip) so there's no extra auth surface — the
bot acts on behalf of a single configured operator account.

Configuration (env vars; bot is a no-op if any required one is missing):
    DISCORD_BOT_TOKEN          — bot token from https://discord.com/developers
    DISCORD_OPERATOR_USER_ID   — only this Discord user can issue mutating commands
    DISCORD_GUILD_ID           — guild (server) where slash commands sync (faster than global)
    DISCORD_OPERATOR_EMAIL     — Trade Copilot user the bot acts as (defaults to first User row)

Commands (slash):
    /status                 — open positions + bot state + balance
    /balance                — broker balance snapshot
    /positions              — current open positions
    /scan                   — run a LaT-PFN multi-instrument scan + post results
    /long  symbol lot [sl tp]  — place a buy
    /short symbol lot [sl tp]  — place a sell
    /close position_id      — close a position by id
    /panic                  — global pause for the operator's bot
    /resume                 — clear panic

Auth model:
    Every command is gated by the caller's Discord user id. Non-operator
    callers get a polite "you're not the operator" reply. The operator's
    Trade Copilot user is resolved once at boot time and cached.

The bot is intentionally tolerant of partial configuration — a wrong
token won't crash the FastAPI app; the bot just logs and exits.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-imported below so a missing discord.py doesn't break imports of
# this module on machines that don't run the bot.
_discord = None


def _require_discord():
    global _discord
    if _discord is None:
        import discord
        _discord = discord
    return _discord


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

class BotConfig:
    """Snapshot of the env config the bot needs. Created once at boot."""
    def __init__(self) -> None:
        self.token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or None
        op_id = os.getenv("DISCORD_OPERATOR_USER_ID", "").strip()
        self.operator_id: Optional[int] = int(op_id) if op_id.isdigit() else None
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.guild_id: Optional[int] = int(guild_id) if guild_id.isdigit() else None
        self.operator_email = os.getenv("DISCORD_OPERATOR_EMAIL", "").strip() or None

    def enabled(self) -> bool:
        # Token is the only hard requirement. Other fields gate specific
        # features (operator_id → command auth; guild_id → fast slash sync).
        return bool(self.token)


# ---------------------------------------------------------------------
# Bot factory + command registration
# ---------------------------------------------------------------------

async def _resolve_operator_user_id(cfg: BotConfig) -> Optional[int]:
    """Look up the Trade Copilot user id the bot will act as.

    Priority: DISCORD_OPERATOR_EMAIL > first User row in the DB. Falls
    back to None if the DB has no users yet — commands that need a user
    context will refuse politely.
    """
    from app.db.database import SessionLocal
    from app.db.models import User

    db = SessionLocal()
    try:
        if cfg.operator_email:
            u = db.query(User).filter(User.email == cfg.operator_email).first()
            if u:
                return int(u.id)
        u = db.query(User).order_by(User.id.asc()).first()
        return int(u.id) if u else None
    finally:
        db.close()


def _build_bot(cfg: BotConfig):
    """Construct the discord.Client + register slash commands."""
    discord = _require_discord()

    intents = discord.Intents.default()
    intents.message_content = False  # we're slash-only

    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)

    @client.event
    async def on_ready():
        try:
            if cfg.guild_id:
                guild = discord.Object(id=cfg.guild_id)
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
                logger.info(
                    "discord_bot ready as %s; synced commands to guild %s",
                    client.user, cfg.guild_id,
                )
            else:
                await tree.sync()
                logger.info(
                    "discord_bot ready as %s; synced commands globally (may take ~1hr)",
                    client.user,
                )
        except Exception as exc:  # noqa: BLE001 — never crash on sync issues
            logger.warning("discord_bot command sync failed: %s", exc)

    # ----- Helpers -------------------------------------------------------

    def _is_operator(interaction) -> bool:
        if cfg.operator_id is None:
            return False
        return int(interaction.user.id) == cfg.operator_id

    async def _operator_user():
        """Return the Trade Copilot User row to act as. None if not configured."""
        uid = await _resolve_operator_user_id(cfg)
        if uid is None:
            return None
        from app.db.database import SessionLocal
        from app.db.models import User
        db = SessionLocal()
        try:
            return db.get(User, uid)
        finally:
            db.close()

    async def _refuse_if_not_operator(interaction) -> bool:
        """Reject mutating commands from non-operator callers."""
        if _is_operator(interaction):
            return False
        await interaction.response.send_message(
            "You're not the configured operator for this bot. "
            "Read-only commands (/status, /balance, /positions, /scan) are open; "
            "trading commands are restricted.",
            ephemeral=True,
        )
        return True

    # ----- Read-only commands -------------------------------------------

    @tree.command(name="status", description="Open positions + bot state")
    async def status(interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        u = await _operator_user()
        if u is None:
            await interaction.followup.send("No operator user configured.", ephemeral=True)
            return
        try:
            from app.core.crypto import decrypt
            from app.core.tradelocker_client import TradeLockerClient
            if not (u.tradelocker_token and u.tradelocker_account_id):
                await interaction.followup.send(
                    "No broker linked. Run /connect on the web app first.",
                    ephemeral=True,
                )
                return
            tok = decrypt(u.tradelocker_token)
            client = TradeLockerClient(env=u.tradelocker_env or "demo")
            state = await client.get_account_state(
                u.tradelocker_account_id, tok or "", u.tradelocker_acc_num or "1"
            )
            positions = await client.get_positions(
                u.tradelocker_account_id, tok or "", u.tradelocker_acc_num or "1"
            )
            bal = state.get("balance", "?")
            equity = state.get("projectedBalance", "?")
            n = len(positions) if isinstance(positions, list) else 0
            await interaction.followup.send(
                f"**{u.email}** ({u.tradelocker_env})\n"
                f"balance: `${bal}` · equity: `${equity}` · open positions: `{n}`\n"
                f"panic: `{'ON' if u.bot_paused else 'off'}` · "
                f"appetite: `{u.risk_appetite}`",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("/status failed: %s", exc)
            await interaction.followup.send(f"status failed: `{exc}`", ephemeral=True)

    @tree.command(name="balance", description="Broker balance snapshot")
    async def balance(interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        u = await _operator_user()
        if u is None or not u.tradelocker_token or not u.tradelocker_account_id:
            await interaction.followup.send("No broker linked.", ephemeral=True)
            return
        try:
            from app.core.crypto import decrypt
            from app.core.tradelocker_client import TradeLockerClient
            tok = decrypt(u.tradelocker_token) or ""
            client = TradeLockerClient(env=u.tradelocker_env or "demo")
            state = await client.get_account_state(
                u.tradelocker_account_id, tok, u.tradelocker_acc_num or "1"
            )
            await interaction.followup.send(
                f"balance: `${state.get('balance', '?')}`\n"
                f"equity: `${state.get('projectedBalance', '?')}`\n"
                f"available: `${state.get('availableFunds', '?')}`\n"
                f"open P&L: `${state.get('openGrossPnL', '?')}`",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"balance failed: `{exc}`", ephemeral=True)

    @tree.command(name="positions", description="List open positions")
    async def positions(interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        u = await _operator_user()
        if u is None or not u.tradelocker_token or not u.tradelocker_account_id:
            await interaction.followup.send("No broker linked.", ephemeral=True)
            return
        try:
            from app.core.crypto import decrypt
            from app.core.tradelocker_client import TradeLockerClient
            tok = decrypt(u.tradelocker_token) or ""
            client = TradeLockerClient(env=u.tradelocker_env or "demo")
            ps = await client.get_positions(
                u.tradelocker_account_id, tok, u.tradelocker_acc_num or "1"
            )
            if not ps:
                await interaction.followup.send("No open positions.", ephemeral=True)
                return
            lines = []
            for p in ps[:10]:
                # POSITION_COL keys: id, side, qty, avgPrice, unrealizedPl,
                # tradableInstrumentId. Symbol-resolution would require an
                # extra round trip per row, so we surface the broker's
                # tradableInstrumentId and let the operator look it up.
                lines.append(
                    f"`{p.get('id', '?')}` · {p.get('side', '?')} "
                    f"tid {p.get('tradableInstrumentId', '?')} · "
                    f"qty {p.get('qty', '?')} · entry {p.get('avgPrice', '?')} "
                    f"· pnl `${p.get('unrealizedPl', '?')}`"
                )
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"positions failed: `{exc}`", ephemeral=True)

    @tree.command(name="scan", description="Run a LaT-PFN multi-instrument scan (~5-10s)")
    async def scan(interaction):
        # Public (not ephemeral) so the result lives in the channel for
        # team review.
        await interaction.response.defer(thinking=True, ephemeral=False)
        try:
            from app.integrations.latpfn_scan import format_table, run_scan
            rows = await run_scan(timeout_s=25.0)
            strong = [r for r in rows if abs(r.snr) >= 1.5]
            highlight = ""
            if strong:
                top = strong[0]
                highlight = (
                    f"\n**Top signal:** {top.label} {top.direction} "
                    f"·  {top.drift_pct:+.2f}% over the next 12h · {top.snr:+.2f}σ\n"
                )
            elif rows:
                top = rows[0]
                highlight = (
                    f"\n_No signal crosses the 1.5σ bar. Strongest: "
                    f"{top.label} {top.direction} ({top.snr:+.2f}σ)._\n"
                )
            await interaction.followup.send(
                f"**LaT-PFN scan** — 12h horizon, ranked by |drift/σ|.{highlight}"
                f"{format_table(rows)}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("/scan failed: %s", exc)
            await interaction.followup.send(f"scan failed: `{exc}`")

    # ----- Mutating commands --------------------------------------------

    @tree.command(name="long", description="Place a BUY order at market")
    async def long_cmd(
        interaction,
        symbol: str,
        lot: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ):
        if await _refuse_if_not_operator(interaction):
            return
        await _place_order(interaction, side="buy", symbol=symbol, lot=lot, sl=sl, tp=tp)

    @tree.command(name="short", description="Place a SELL order at market")
    async def short_cmd(
        interaction,
        symbol: str,
        lot: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ):
        if await _refuse_if_not_operator(interaction):
            return
        await _place_order(interaction, side="sell", symbol=symbol, lot=lot, sl=sl, tp=tp)

    @tree.command(name="close", description="Close a position by id")
    async def close_cmd(interaction, position_id: str):
        if await _refuse_if_not_operator(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        u = await _operator_user()
        if u is None or not u.tradelocker_token or not u.tradelocker_account_id:
            await interaction.followup.send("No broker linked.", ephemeral=True)
            return
        try:
            from app.core.crypto import decrypt
            from app.core.tradelocker_client import TradeLockerClient
            tok = decrypt(u.tradelocker_token) or ""
            client = TradeLockerClient(env=u.tradelocker_env or "demo")
            # close_position signature: (position_id, token, acc_num) —
            # TradeLocker's close endpoint is broker-wide, not account-scoped.
            await client.close_position(position_id, tok, u.tradelocker_acc_num or "1")
            await interaction.followup.send(f"Closed position `{position_id}`.", ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"close failed: `{exc}`", ephemeral=True)

    @tree.command(name="panic", description="Pause ALL bot trading for the operator")
    async def panic_cmd(interaction):
        if await _refuse_if_not_operator(interaction):
            return
        await _set_panic(interaction, paused=True)

    @tree.command(name="resume", description="Clear panic and resume bot trading")
    async def resume_cmd(interaction):
        if await _refuse_if_not_operator(interaction):
            return
        await _set_panic(interaction, paused=False)

    # ----- Mutating helpers ---------------------------------------------

    async def _place_order(interaction, *, side, symbol, lot, sl, tp):
        await interaction.response.defer(thinking=True, ephemeral=True)
        u = await _operator_user()
        if u is None or not u.tradelocker_token or not u.tradelocker_account_id:
            await interaction.followup.send("No broker linked.", ephemeral=True)
            return
        try:
            from app.core.crypto import decrypt
            from app.core.tradelocker_client import TradeLockerClient
            tok = decrypt(u.tradelocker_token) or ""
            client = TradeLockerClient(env=u.tradelocker_env or "demo")
            order = await client.place_order(
                account_id=u.tradelocker_account_id,
                token=tok,
                acc_num=u.tradelocker_acc_num or "1",
                symbol=symbol.upper(),
                side=side,
                qty=float(lot),
                sl=float(sl) if sl is not None else None,
                tp=float(tp) if tp is not None else None,
                client_order_id=f"dc-{interaction.id}",
            )
            await interaction.followup.send(
                f"✅ {side.upper()} {symbol.upper()} `{lot}` placed.\n"
                f"order: `{order.get('order_id') or order.get('orderId') or order}`",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"order failed: `{exc}`", ephemeral=True)

    async def _set_panic(interaction, *, paused: bool):
        await interaction.response.defer(thinking=True, ephemeral=True)
        from app.db.database import SessionLocal
        from app.db.models import User
        u = await _operator_user()
        if u is None:
            await interaction.followup.send("No operator user.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            row = db.get(User, u.id)
            if row is None:
                await interaction.followup.send("User missing.", ephemeral=True)
                return
            row.bot_paused = bool(paused)
            db.commit()
            await interaction.followup.send(
                f"🛑 panic ON — all bot trading paused for `{row.email}`."
                if paused else f"▶ panic cleared — bot trading resumed for `{row.email}`.",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            await interaction.followup.send(f"panic toggle failed: `{exc}`", ephemeral=True)
        finally:
            db.close()

    return client


# ---------------------------------------------------------------------
# Lifespan integration
# ---------------------------------------------------------------------

_bot_task: Optional[asyncio.Task] = None
_bot_client = None


async def start_bot() -> None:
    """Start the bot in the background. No-op if env not configured.

    Called once from app.main.lifespan(). Stores the task globally so
    shutdown can cancel it cleanly.
    """
    global _bot_task, _bot_client
    cfg = BotConfig()
    if not cfg.enabled():
        logger.info("discord_bot: DISCORD_BOT_TOKEN not set; bot disabled")
        return
    try:
        client = _build_bot(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord_bot: build failed (%s) — disabling", exc)
        return
    _bot_client = client

    async def _run():
        try:
            assert cfg.token is not None  # narrowed by cfg.enabled()
            await client.start(cfg.token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord_bot: gateway loop crashed: %s", exc)

    _bot_task = asyncio.create_task(_run(), name="discord_bot")
    logger.info("discord_bot: starting (guild=%s, operator=%s)",
                cfg.guild_id, cfg.operator_id)


async def stop_bot() -> None:
    """Close the bot cleanly on app shutdown."""
    global _bot_task, _bot_client
    if _bot_client is not None:
        try:
            await _bot_client.close()
        except Exception:  # noqa: BLE001
            pass
        _bot_client = None
    if _bot_task is not None:
        _bot_task.cancel()
        try:
            await _bot_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _bot_task = None
