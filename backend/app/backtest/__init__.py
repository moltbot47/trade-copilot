"""Walk-forward backtest harness for strategy validation.

Replays historical OHLCV bars through the production strategy +
TradeManager state machine to validate strategy changes without
live trading. See app.backtest.engine.BacktestEngine.
"""
from app.backtest.engine import BacktestEngine
from app.backtest.results import BacktestResult

__all__ = ["BacktestEngine", "BacktestResult"]
