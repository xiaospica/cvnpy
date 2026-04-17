"""Adapter 单元测试.

目的: 不依赖真实的 vnpy 策略引擎, 用 FakeEngine 验证适配层能正确吸收:
    - CtaEngine 的 4 参数 add_strategy + Future init_strategy + bool remove_strategy
    - SignalEnginePlus 的 1 参数 add_strategy + bool init_strategy + None remove_strategy
以及 StrategyInfo 快照生成, 能力集判断, 错误路径 (策略不存在/引擎不支持 edit)。
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Dict

import pytest

from vnpy_webtrader.strategy_adapter import (
    AddStrategyRequest,
    CtaStrategyAdapter,
    SignalStrategyPlusAdapter,
    StrategyEngineAdapter,
    StrategyInfo,
    StrategyOpResult,
    build_adapters,
)


# ---------------------------------------------------------------------------
# Fake strategy instance
# ---------------------------------------------------------------------------


class FakeStrategy:
    parameters = ["alpha", "beta"]
    variables = ["last_signal_id"]

    def __init__(self, name: str, vt_symbol: str = "") -> None:
        self.strategy_name = name
        self.vt_symbol = vt_symbol
        self.author = "test"
        self.inited = False
        self.trading = False
        self.alpha = 1
        self.beta = "two"
        self.last_signal_id = 0
        self._settings: Dict[str, Any] = {}

    def get_parameters(self) -> dict:
        return {"alpha": self.alpha, "beta": self.beta}

    def get_variables(self) -> dict:
        return {"last_signal_id": self.last_signal_id}

    def update_setting(self, setting: dict) -> None:
        self._settings.update(setting)


# ---------------------------------------------------------------------------
# Fake engines
# ---------------------------------------------------------------------------


class FakeSignalEngine:
    """模仿 SignalEnginePlus: ``add_strategy(class_name)`` + ``init/remove`` 同步返回."""

    def __init__(self) -> None:
        self.strategies: Dict[str, FakeStrategy] = {}
        self.classes = {"ClassA": FakeStrategy}

    def get_all_strategy_class_names(self):
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str):
        return {"alpha": 1, "beta": "two"}

    def get_strategy_parameters(self, name: str):
        return self.strategies[name].get_parameters()

    def add_strategy(self, class_name: str) -> None:
        s = FakeStrategy("multistrategy-v5.2.1")
        self.strategies[s.strategy_name] = s

    def init_strategy(self, name: str) -> bool:
        s = self.strategies[name]
        s.inited = True
        return True

    def start_strategy(self, name: str) -> None:
        self.strategies[name].trading = True

    def stop_strategy(self, name: str) -> None:
        self.strategies[name].trading = False

    def remove_strategy(self, name: str) -> None:
        self.strategies.pop(name)

    def put_strategy_event(self, strategy: FakeStrategy) -> None:
        pass

    def init_all_strategies(self):
        for n in self.strategies:
            self.init_strategy(n)

    def start_all_strategies(self):
        for n in self.strategies:
            self.start_strategy(n)

    def stop_all_strategies(self):
        for n in self.strategies:
            self.stop_strategy(n)


class FakeCtaEngine:
    """模仿 CtaEngine: 4 参数 add_strategy + Future init_strategy + bool remove_strategy."""

    def __init__(self) -> None:
        self.strategies: Dict[str, FakeStrategy] = {}
        self.classes = {"MaCross": FakeStrategy}

    def get_all_strategy_class_names(self):
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str):
        return {"alpha": 1, "beta": "two"}

    def get_strategy_parameters(self, name: str):
        return self.strategies[name].get_parameters()

    def add_strategy(self, class_name: str, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        s = FakeStrategy(strategy_name, vt_symbol=vt_symbol)
        s._settings.update(setting)
        self.strategies[strategy_name] = s

    def init_strategy(self, name: str) -> Future:
        fut: Future = Future()
        s = self.strategies[name]
        s.inited = True
        fut.set_result(None)
        return fut

    def start_strategy(self, name: str) -> None:
        self.strategies[name].trading = True

    def stop_strategy(self, name: str) -> None:
        self.strategies[name].trading = False

    def edit_strategy(self, name: str, setting: dict) -> None:
        self.strategies[name].update_setting(setting)

    def remove_strategy(self, name: str) -> bool:
        if self.strategies[name].trading:
            return False
        self.strategies.pop(name)
        return True

    def init_all_strategies(self):
        for n in self.strategies:
            self.init_strategy(n)

    def start_all_strategies(self):
        for n in self.strategies:
            self.start_strategy(n)

    def stop_all_strategies(self):
        for n in self.strategies:
            self.stop_strategy(n)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_signal_plus_add_init_start_stop_remove_cycle():
    adapter = SignalStrategyPlusAdapter(FakeSignalEngine())
    assert "add" in adapter.capabilities

    result = adapter.add_strategy(
        AddStrategyRequest(
            engine="SignalStrategyPlus",
            class_name="ClassA",
            strategy_name="multistrategy-v5.2.1",
            setting={"alpha": 9},
        )
    )
    assert result.ok, result.message
    assert "multistrategy-v5.2.1" in adapter.engine.strategies

    # setting 应当被应用
    assert adapter.engine.strategies["multistrategy-v5.2.1"]._settings == {"alpha": 9}

    assert adapter.init_strategy("multistrategy-v5.2.1").ok
    assert adapter.engine.strategies["multistrategy-v5.2.1"].inited is True

    assert adapter.start_strategy("multistrategy-v5.2.1").ok
    info = adapter.get_strategy("multistrategy-v5.2.1")
    assert info is not None
    assert info.trading is True
    assert info.parameters == {"alpha": 1, "beta": "two"}

    assert adapter.stop_strategy("multistrategy-v5.2.1").ok
    assert adapter.remove_strategy("multistrategy-v5.2.1").ok
    assert adapter.get_strategy("multistrategy-v5.2.1") is None


def test_cta_add_requires_vt_symbol():
    adapter = CtaStrategyAdapter(FakeCtaEngine())

    # 缺 vt_symbol 应当失败
    bad = adapter.add_strategy(
        AddStrategyRequest(
            engine="CtaStrategy",
            class_name="MaCross",
            strategy_name="ma_001",
            setting={"alpha": 5},
        )
    )
    assert not bad.ok
    assert "vt_symbol" in bad.message

    ok = adapter.add_strategy(
        AddStrategyRequest(
            engine="CtaStrategy",
            class_name="MaCross",
            strategy_name="ma_001",
            vt_symbol="600000.SSE",
            setting={"alpha": 5},
        )
    )
    assert ok.ok


def test_cta_init_unwraps_future():
    """CtaEngine.init_strategy 返回 Future, 适配器应同步解包成 StrategyOpResult."""
    adapter = CtaStrategyAdapter(FakeCtaEngine())
    adapter.add_strategy(
        AddStrategyRequest(
            engine="CtaStrategy",
            class_name="MaCross",
            strategy_name="ma_001",
            vt_symbol="600000.SSE",
            setting={},
        )
    )
    result = adapter.init_strategy("ma_001")
    assert isinstance(result, StrategyOpResult)
    assert result.ok
    assert adapter.engine.strategies["ma_001"].inited is True


def test_cta_remove_returns_false_when_trading():
    engine = FakeCtaEngine()
    adapter = CtaStrategyAdapter(engine)
    adapter.add_strategy(
        AddStrategyRequest(
            engine="CtaStrategy",
            class_name="MaCross",
            strategy_name="ma_001",
            vt_symbol="600000.SSE",
            setting={},
        )
    )
    adapter.init_strategy("ma_001")
    adapter.start_strategy("ma_001")
    result = adapter.remove_strategy("ma_001")
    assert not result.ok  # CtaEngine 要求先停止才能删


def test_adapter_rejects_missing_strategy():
    adapter = SignalStrategyPlusAdapter(FakeSignalEngine())
    result = adapter.start_strategy("nonexistent")
    assert not result.ok
    assert "不存在" in result.message or "not" in result.message.lower()


def test_list_strategies_snapshot_fields():
    adapter = SignalStrategyPlusAdapter(FakeSignalEngine())
    adapter.add_strategy(
        AddStrategyRequest(
            engine="SignalStrategyPlus",
            class_name="ClassA",
            strategy_name="multistrategy-v5.2.1",
        )
    )
    infos = adapter.list_strategies()
    assert len(infos) == 1
    info = infos[0]
    assert isinstance(info, StrategyInfo)
    assert info.engine == "SignalStrategyPlus"
    assert info.class_name == "FakeStrategy"
    assert info.inited is False and info.trading is False
    assert info.parameters == {"alpha": 1, "beta": "two"}
    # vt_symbol 为空应归一成 None
    assert info.vt_symbol is None


def test_describe_reports_capabilities():
    adapter = CtaStrategyAdapter(FakeCtaEngine())
    desc = adapter.describe()
    assert desc["app_name"] == "CtaStrategy"
    assert "edit" in desc["capabilities"]
    assert desc["event_type"] == "eCtaStrategy"


def test_build_adapters_matches_engines_by_app_name():
    class FakeMainEngine:
        engines = {
            "CtaStrategy": FakeCtaEngine(),
            "SignalStrategyPlus": FakeSignalEngine(),
            "OmsEngine": object(),
        }

    adapters = build_adapters(FakeMainEngine())
    assert set(adapters.keys()) == {"CtaStrategy", "SignalStrategyPlus"}
    assert isinstance(adapters["CtaStrategy"], CtaStrategyAdapter)
    assert isinstance(adapters["SignalStrategyPlus"], SignalStrategyPlusAdapter)


def test_signal_plus_edit_updates_setting():
    adapter = SignalStrategyPlusAdapter(FakeSignalEngine())
    adapter.add_strategy(
        AddStrategyRequest(
            engine="SignalStrategyPlus",
            class_name="ClassA",
            strategy_name="multistrategy-v5.2.1",
        )
    )
    result = adapter.edit_strategy("multistrategy-v5.2.1", {"alpha": 42})
    assert result.ok
    assert adapter.engine.strategies["multistrategy-v5.2.1"]._settings == {"alpha": 42}
