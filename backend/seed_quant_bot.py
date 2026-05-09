"""Seed the LaT-PFN Quant Trader bot + StrategyState rows.

Idempotent: re-running won't duplicate rows.
"""
from __future__ import annotations

from app.db.database import Base, SessionLocal, engine
from app.db.models import Bot, StrategyState, StrategyType


BOT_CFG = dict(
    name="LaT-PFN Quant Trader",
    slug="latpfn-quant",
    description=(
        "Expert Quant Trader: pyramiding (scale-in) on confirmed momentum, "
        "scale-out 50% at +1R for break-even risk, ATR trailing stop after "
        "partial close. Built on top of LaT-PFN forecasts."
    ),
    strategy_type=StrategyType.latpfn_quant,
    backtest_win_rate=0.0,
    backtest_profit_factor=0.0,
    risk_level=4,
    instruments_csv="BTCUSD,ETHUSD",
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
            bot = Bot(**BOT_CFG)
            db.add(bot)
            db.flush()
            inserted_bot = True
        else:
            bot.name = BOT_CFG["name"]
            bot.description = BOT_CFG["description"]
            bot.strategy_type = BOT_CFG["strategy_type"]
            bot.instruments_csv = BOT_CFG["instruments_csv"]
            bot.risk_level = BOT_CFG["risk_level"]
            if not getattr(bot, "webhook_secret", None):
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
