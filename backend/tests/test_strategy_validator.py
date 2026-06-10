"""Tests for the AST safety scan of uploaded partner strategy source."""
from app.core.strategy_validator import validate_strategy_source

CLEAN = '''
from __future__ import annotations
from typing import Optional
import pandas as pd
import numpy as np
import math
from app.strategies.base import Strategy, StrategySignal


class VelocitySpike(Strategy):
    name = "velocity_spike"
    timeframe = "1m"

    def __init__(self, *, params=None):
        self.params = params or {}

    async def on_bar(self, symbol, bars):
        if math.isnan(bars["close"].iloc[-1]):
            return None
        return None
'''


def _codes(result):
    return {f.code for f in result.findings}


def test_clean_strategy_passes():
    r = validate_strategy_source(CLEAN, expected_name="velocity_spike")
    assert r.ok, r.to_dict()
    assert r.strategy_class == "VelocitySpike"
    assert r.declared_name == "velocity_spike"


def test_blocks_import_os():
    r = validate_strategy_source(CLEAN + "\nimport os\n")
    assert not r.ok
    assert "import_not_allowed" in _codes(r)


def test_blocks_subprocess_from_import():
    src = CLEAN + "\nfrom subprocess import run\n"
    r = validate_strategy_source(src)
    assert not r.ok
    assert "import_not_allowed" in _codes(r)


def test_blocks_eval_exec_open():
    for call in ("eval('1')", "exec('x=1')", "open('/etc/passwd')", "__import__('os')"):
        src = CLEAN + f"\nx = {call}\n"
        r = validate_strategy_source(src)
        assert not r.ok, call
        assert _codes(r) & {"forbidden_call", "forbidden_name"}, call


def test_blocks_dunder_escape():
    src = CLEAN + "\nleak = ().__class__.__bases__[0].__subclasses__()\n"
    r = validate_strategy_source(src)
    assert not r.ok
    assert "forbidden_attribute" in _codes(r)


def test_blocks_getattr():
    src = CLEAN + "\ny = getattr(object, 'x', None)\n"
    r = validate_strategy_source(src)
    assert not r.ok
    assert "forbidden_call" in _codes(r)


def test_blocks_relative_import():
    src = CLEAN + "\nfrom . import sibling\n"
    r = validate_strategy_source(src)
    assert not r.ok
    assert "relative_import" in _codes(r)


def test_blocks_syntax_error():
    r = validate_strategy_source("def broken(:\n  pass")
    assert not r.ok
    assert "syntax_error" in _codes(r)


def test_blocks_missing_strategy_class():
    src = "import pandas as pd\nx = 1\n"
    r = validate_strategy_source(src)
    assert not r.ok
    assert "no_strategy_class" in _codes(r)


def test_blocks_missing_on_bar():
    src = '''
from app.strategies.base import Strategy

class NoBar(Strategy):
    name = "nobar"
'''
    r = validate_strategy_source(src)
    assert not r.ok
    assert "missing_on_bar" in _codes(r)


def test_blocks_missing_name():
    src = '''
from app.strategies.base import Strategy

class NoName(Strategy):
    async def on_bar(self, symbol, bars):
        return None
'''
    r = validate_strategy_source(src)
    assert not r.ok
    assert "missing_name" in _codes(r)


def test_blocks_name_mismatch():
    r = validate_strategy_source(CLEAN, expected_name="something_else")
    assert not r.ok
    assert "name_mismatch" in _codes(r)


def test_blocks_multiple_strategy_classes():
    src = CLEAN + '''
class Second(Strategy):
    name = "second"
    async def on_bar(self, symbol, bars):
        return None
'''
    r = validate_strategy_source(src)
    assert not r.ok
    assert "multiple_strategy_classes" in _codes(r)


def test_empty_source_blocked():
    r = validate_strategy_source("   ")
    assert not r.ok
    assert "empty" in _codes(r)


def test_oversized_source_blocked():
    r = validate_strategy_source("x = 1\n" * 40_000)
    assert not r.ok
    assert "too_large" in _codes(r)
