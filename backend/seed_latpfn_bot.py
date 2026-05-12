"""Seed the LaT-PFN Momentum bot + StrategyState rows for 1m and 5m.

Idempotent: re-running won't duplicate rows.
"""
from __future__ import annotations

from app.db.database import Base, SessionLocal, engine
from app.db.models import Bot, StrategyState, StrategyType


BOT_CFG = dict(
    name="LaT-PFN Momentum",
    slug="latpfn-momentum",
    description=(
        "Probabilistic momentum from LaT-PFN zero-shot forecasting. "
        "Fires when forecast drift exceeds a confidence threshold (σ-units) "
        "and auto-tunes itself from rolling performance."
    ),
    strategy_type=StrategyType.latpfn_momentum,
    backtest_win_rate=0.0,
    backtest_profit_factor=0.0,
    risk_level=4,
    # SP500 added 2026-05-11 — cheapest index margin (~$3.72 at 0.01 lot)
    # making it ideal for tiny-account compounding.
    instruments_csv="BTCUSD,ETHUSD,NAS100,EURUSD,SP500",
)

TIMEFRAMES = ["1m", "5m"]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    inserted_bot = False
    inserted_states = 0
    try:
        bot = db.query(Bot).filter(Bot.slug == BOT_CFG["slug"]).first()
        if bot is None:
            # Bot.webhook_secret default factory fires here.
            bot = Bot(**BOT_CFG)
            db.add(bot)
            db.flush()  # so bot.id is populated
            inserted_bot = True
        else:
            # Keep the existing bot but refresh metadata (idempotent).
            bot.name = BOT_CFG["name"]
            bot.description = BOT_CFG["description"]
            bot.strategy_type = BOT_CFG["strategy_type"]
            bot.instruments_csv = BOT_CFG["instruments_csv"]
            bot.risk_level = BOT_CFG["risk_level"]
            if not getattr(bot, "webhook_secret", None):
                # Heal a row that pre-dates the webhook_secret column.
                from app.db.models import _generate_webhook_secret

                bot.webhook_secret = _generate_webhook_secret()

        for tf in TIMEFRAMES:
            existing = (
                db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == bot.id,
                    StrategyState.timeframe == tf,
                )
                .first()
            )
            if existing is None:
                db.add(
                    StrategyState(
                        bot_id=bot.id,
                        timeframe=tf,
                        is_running=False,
                        confidence_threshold=1.5,
                        max_concurrent=3,
                    )
                )
                inserted_states += 1

        db.commit()
        print(
            f"seed complete: bot_inserted={inserted_bot} "
            f"new_states={inserted_states} bot_id={bot.id} slug={bot.slug}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
