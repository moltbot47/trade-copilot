"""Standalone seeder. Idempotently inserts the 3 starter bots into the DB."""
from __future__ import annotations

from app.db.database import Base, SessionLocal, engine
from app.db.models import Bot, StrategyType


SEED = [
    dict(
        name="ORB Breakout",
        slug="orb-breakout",
        description="Opening Range Breakout - trades the first 30-min range break on indices and gold.",
        strategy_type=StrategyType.orb,
        backtest_win_rate=62.0,
        backtest_profit_factor=1.4,
        risk_level=3,
        instruments_csv="NAS100,SP500,XAUUSD",
    ),
    dict(
        name="Squeeze Momentum",
        slug="squeeze-momentum",
        description="Bollinger/Keltner squeeze release - rides volatility expansion on FX and indices.",
        strategy_type=StrategyType.squeeze,
        backtest_win_rate=58.0,
        backtest_profit_factor=1.6,
        risk_level=4,
        instruments_csv="EURUSD,USDJPY,NAS100",
    ),
    dict(
        name="Stoch Hook Reversal",
        slug="stoch-hook-reversal",
        description="Stochastic hook on overbought/oversold for mean-reversion entries.",
        strategy_type=StrategyType.stoch_hook,
        backtest_win_rate=65.0,
        backtest_profit_factor=1.3,
        risk_level=2,
        instruments_csv="XAUUSD,EURUSD",
    ),
]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    inserted = 0
    try:
        for cfg in SEED:
            if not db.query(Bot).filter(Bot.slug == cfg["slug"]).first():
                db.add(Bot(**cfg))
                inserted += 1
        db.commit()
    finally:
        db.close()
    print(f"seed complete: inserted {inserted} new bot(s); total seeds: {len(SEED)}")


if __name__ == "__main__":
    main()
