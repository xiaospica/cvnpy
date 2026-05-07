"""策略引擎通用适配层。

节点侧的 WebEngine 通过本模块访问任意策略引擎 (CtaEngine / SignalEnginePlus 等),
屏蔽不同引擎在 ``add_strategy`` / ``init_strategy`` / ``remove_strategy`` 等方法上的
签名差异, 使得上层 REST 路由只需面对一套统一接口。

新增引擎只需:
    1. 写一个继承 ``StrategyEngineAdapter`` 的子类;
    2. 在 ``ADAPTER_REGISTRY`` 中按 ``APP_NAME`` 注册。
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Set, Type


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass
class StrategyInfo:
    """策略实例的标准化快照, 作为 REST/WS 的对外数据结构。"""

    engine: str
    name: str
    class_name: str
    vt_symbol: Optional[str]
    author: Optional[str]
    inited: bool
    trading: bool
    parameters: Dict[str, Any]
    variables: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyOpResult:
    """策略写操作 (init/start/stop/...) 的统一返回值。"""

    ok: bool
    message: str = ""
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "data": self.data}


@dataclass
class AddStrategyRequest:
    """创建策略实例的统一入参 (全集字段, 各适配器按需取用)。"""

    engine: str
    class_name: str
    strategy_name: str
    vt_symbol: Optional[str] = None
    setting: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AddStrategyRequest":
        return cls(
            engine=data["engine"],
            class_name=data["class_name"],
            strategy_name=data["strategy_name"],
            vt_symbol=data.get("vt_symbol"),
            setting=data.get("setting") or {},
        )


# ---------------------------------------------------------------------------
# 适配器抽象基类
# ---------------------------------------------------------------------------


class StrategyEngineAdapter:
    """策略引擎适配器基类。子类实现对具体引擎的封装。"""

    #: 引擎的 APP_NAME, 与 ``main_engine.get_engine(app_name)`` 对应
    app_name: str = ""
    #: 面向用户的显示名
    display_name: str = ""
    #: 该引擎发出的策略状态事件名 (供 WebEngine 订阅后向 WS 广播)
    event_type: str = ""
    #: 支持的能力集合。写路由根据它决定按钮可用性, 不支持的操作返回 501
    default_capabilities: Set[str] = frozenset(
        {"add", "init", "start", "stop", "remove"}
    )

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.capabilities: Set[str] = set(self.default_capabilities)

    # ---- 元信息 -----------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        return {
            "app_name": self.app_name,
            "display_name": self.display_name or self.app_name,
            "event_type": self.event_type,
            "capabilities": sorted(self.capabilities),
        }

    # ---- 类与参数查询 -----------------------------------------------------

    def list_classes(self) -> List[str]:
        return list(self.engine.get_all_strategy_class_names())

    def get_class_params(self, class_name: str) -> Dict[str, Any]:
        return dict(self.engine.get_strategy_class_parameters(class_name))

    # ---- 实例查询 ---------------------------------------------------------

    def list_strategies(self) -> List[StrategyInfo]:
        result: List[StrategyInfo] = []
        for name, strategy in self._iter_strategies():
            result.append(self._snapshot(name, strategy))
        return result

    def get_strategy(self, name: str) -> Optional[StrategyInfo]:
        strategy = self._get_strategy_obj(name)
        if strategy is None:
            return None
        return self._snapshot(name, strategy)

    # ---- 写操作 -----------------------------------------------------------

    def add_strategy(self, req: AddStrategyRequest) -> StrategyOpResult:
        raise NotImplementedError

    def init_strategy(self, name: str) -> StrategyOpResult:
        if not self._exists(name):
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        try:
            ret = self.engine.init_strategy(name)
        except Exception as exc:  # pragma: no cover - defensive
            return StrategyOpResult(False, f"初始化异常: {exc}")
        return self._normalize_init_result(ret)

    def start_strategy(self, name: str) -> StrategyOpResult:
        if not self._exists(name):
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        try:
            self.engine.start_strategy(name)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"启动异常: {exc}")
        return StrategyOpResult(True, "started")

    def stop_strategy(self, name: str) -> StrategyOpResult:
        if not self._exists(name):
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        try:
            self.engine.stop_strategy(name)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"停止异常: {exc}")
        return StrategyOpResult(True, "stopped")

    def remove_strategy(self, name: str) -> StrategyOpResult:
        if not self._exists(name):
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        try:
            ret = self.engine.remove_strategy(name)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"移除异常: {exc}")
        ok = True if ret is None else bool(ret)
        return StrategyOpResult(ok, "removed" if ok else "策略移除失败, 请检查是否仍在运行")

    def edit_strategy(self, name: str, setting: Dict[str, Any]) -> StrategyOpResult:
        return StrategyOpResult(False, "该引擎不支持 edit_strategy", data={"http_status": 501})

    def init_all(self) -> StrategyOpResult:
        try:
            self.engine.init_all_strategies()
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"批量初始化异常: {exc}")
        return StrategyOpResult(True, "init-all dispatched")

    def start_all(self) -> StrategyOpResult:
        try:
            self.engine.start_all_strategies()
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"批量启动异常: {exc}")
        return StrategyOpResult(True, "start-all dispatched")

    def stop_all(self) -> StrategyOpResult:
        try:
            self.engine.stop_all_strategies()
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"批量停止异常: {exc}")
        return StrategyOpResult(True, "stop-all dispatched")

    # ---- 子类可复写的工具 -------------------------------------------------

    def _iter_strategies(self):
        """遍历引擎内的策略 ``(name, instance)``。"""
        strategies = getattr(self.engine, "strategies", {})
        return list(strategies.items())

    def _get_strategy_obj(self, name: str) -> Any:
        strategies = getattr(self.engine, "strategies", {})
        return strategies.get(name)

    def _exists(self, name: str) -> bool:
        return self._get_strategy_obj(name) is not None

    def _snapshot(self, name: str, strategy: Any) -> StrategyInfo:
        parameters: Dict[str, Any] = {}
        variables: Dict[str, Any] = {}

        if hasattr(strategy, "get_parameters"):
            try:
                parameters = dict(strategy.get_parameters())
            except Exception:
                parameters = {}
        if hasattr(strategy, "get_variables"):
            try:
                variables = dict(strategy.get_variables())
            except Exception:
                variables = {}

        return StrategyInfo(
            engine=self.app_name,
            name=getattr(strategy, "strategy_name", name),
            class_name=strategy.__class__.__name__,
            vt_symbol=getattr(strategy, "vt_symbol", None) or None,
            author=getattr(strategy, "author", "") or None,
            inited=bool(getattr(strategy, "inited", False)),
            trading=bool(getattr(strategy, "trading", False)),
            parameters=parameters,
            variables=variables,
        )

    def _normalize_init_result(self, ret: Any) -> StrategyOpResult:
        """把 ``init_strategy`` 的各种返回值 (None/bool/Future) 统一成 ``StrategyOpResult``."""
        if isinstance(ret, Future):
            try:
                fret = ret.result(timeout=30)
            except Exception as exc:
                return StrategyOpResult(False, f"初始化失败: {exc}")
            if fret is False:
                return StrategyOpResult(False, "初始化返回 False")
            return StrategyOpResult(True, "inited")
        if ret is False:
            return StrategyOpResult(False, "初始化返回 False")
        return StrategyOpResult(True, "inited")

    # ---- 回放权益快照 (引擎无关, 读本地 replay_history.db) -----------------

    def get_replay_equity_snapshots(
        self,
        name: str,
        since: Optional[str] = None,
        limit: int = 10000,
    ) -> List[Dict[str, Any]]:
        """读本地 replay_history.db 回放权益快照 (A1/B2 解耦).

        所有引擎通用 — 只要策略调用过 vnpy_ml_strategy.replay_history.write_snapshot,
        mlearnweb 的 replay_equity_sync_service 就能通过本接口拉到数据.
        """
        try:
            from vnpy_ml_strategy.replay_history import list_snapshots
        except ImportError:
            return []
        return list_snapshots(name, since_iso=since, limit=limit)

    # ---- 通用健康检查 -------------------------------------------------------

    def get_health(self) -> Dict[str, Any]:
        """所有策略的存活状态汇总. ML 子类可 override 追加 ML 专属字段."""
        strategies = getattr(self.engine, "strategies", {})
        result: List[Dict[str, Any]] = []
        for name, obj in strategies.items():
            result.append({
                "name": name,
                "engine": self.app_name,
                "inited": bool(getattr(obj, "inited", False)),
                "trading": bool(getattr(obj, "trading", False)),
            })
        return {"strategies": result}


# ---------------------------------------------------------------------------
# 具体适配器
# ---------------------------------------------------------------------------


class CtaStrategyAdapter(StrategyEngineAdapter):
    """对接 ``vnpy_ctastrategy.CtaEngine``. 其 ``add_strategy`` 要求 4 个位置参数。"""

    app_name = "CtaStrategy"
    display_name = "CTA策略"
    event_type = "eCtaStrategy"
    default_capabilities = frozenset(
        {"add", "init", "start", "stop", "remove", "edit"}
    )

    def add_strategy(self, req: AddStrategyRequest) -> StrategyOpResult:
        if not req.vt_symbol:
            return StrategyOpResult(False, "CtaStrategy 引擎要求必填 vt_symbol")
        try:
            self.engine.add_strategy(
                req.class_name, req.strategy_name, req.vt_symbol, req.setting
            )
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"创建策略异常: {exc}")
        if req.strategy_name not in getattr(self.engine, "strategies", {}):
            return StrategyOpResult(False, "创建失败, 引擎未登记该实例, 详情见日志")
        return StrategyOpResult(True, "added")

    def edit_strategy(self, name: str, setting: Dict[str, Any]) -> StrategyOpResult:
        if not self._exists(name):
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        try:
            self.engine.edit_strategy(name, setting)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"编辑异常: {exc}")
        return StrategyOpResult(True, "edited")


class SignalStrategyPlusAdapter(StrategyEngineAdapter):
    """对接 ``vnpy_signal_strategy_plus.SignalEnginePlus``.

    该引擎 ``add_strategy`` 仅接受 ``class_name`` (或类本身), 策略实例由 class 自身的
    ``strategy_name`` 决定; ``setting`` 需要通过 ``strategy.update_setting`` 应用。
    """

    app_name = "SignalStrategyPlus"
    display_name = "Signal策略Plus"
    event_type = "EVENT_SIGNAL_STRATEGY_PLUS"
    default_capabilities = frozenset(
        {"add", "init", "start", "stop", "remove", "edit"}
    )

    def add_strategy(self, req: AddStrategyRequest) -> StrategyOpResult:
        try:
            self.engine.add_strategy(req.class_name)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"创建策略异常: {exc}")

        # SignalEnginePlus 以策略类里硬编码的 strategy_name 为键, 这里找回实例
        strategies = getattr(self.engine, "strategies", {})
        created = None
        if req.strategy_name and req.strategy_name in strategies:
            created = strategies[req.strategy_name]
        else:
            # 退化到按 class_name 匹配最近创建的实例
            for inst in strategies.values():
                if inst.__class__.__name__ == req.class_name:
                    created = inst
        if created is None:
            return StrategyOpResult(False, "创建失败, 引擎未登记对应实例, 详情见日志")

        if req.setting and hasattr(created, "update_setting"):
            try:
                created.update_setting(req.setting)
            except Exception as exc:
                return StrategyOpResult(False, f"参数应用失败: {exc}")

        return StrategyOpResult(
            True, "added", data={"strategy_name": created.strategy_name}
        )

    def edit_strategy(self, name: str, setting: Dict[str, Any]) -> StrategyOpResult:
        strategy = self._get_strategy_obj(name)
        if strategy is None:
            return StrategyOpResult(False, f"策略实例不存在: {name}")
        if not hasattr(strategy, "update_setting"):
            return StrategyOpResult(False, "策略不支持 update_setting", data={"http_status": 501})
        try:
            strategy.update_setting(setting)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"编辑异常: {exc}")
        if hasattr(self.engine, "put_strategy_event"):
            try:
                self.engine.put_strategy_event(strategy)
            except Exception:  # pragma: no cover
                pass
        return StrategyOpResult(True, "edited")


class LegacySignalStrategyAdapter(SignalStrategyPlusAdapter):
    """对接旧版 ``vnpy_signal_strategy.SignalEngine``, 行为与 Plus 版一致。"""

    app_name = "SignalStrategy"
    display_name = "Signal策略"
    event_type = "eSignalStrategy"


class MLStrategyAdapter(StrategyEngineAdapter):
    """对接 ``vnpy_ml_strategy.MLEngine`` —— ML 日频策略适配器.

    除了 StrategyEngineAdapter 的通用读写操作外, 另外暴露 ML 专属查询:

    * ``get_latest_metrics(name)``     — 最新一日监控指标 dict
    * ``get_metrics_history(name, n)`` — 最近 N 日指标列表
    * ``get_health()``                 — 策略存活/最新跑时间/last_error
    """

    app_name = "MlStrategy"
    display_name = "ML策略"
    event_type = "eMlStrategy"
    default_capabilities = frozenset(
        {"add", "init", "start", "stop", "remove"}
    )

    # ---- 创建策略实例 ----

    def add_strategy(self, req: AddStrategyRequest) -> StrategyOpResult:
        """MLEngine.strategies 是由策略对象自己 register 的, 这里委托给
        引擎的 ``add_strategy`` 方法 (若存在) 或返回未实现提示.

        完整 CRUD 在后续迭代补齐; Phase 2.6 重点是读路径 (metrics 查询).
        """
        if not hasattr(self.engine, "add_strategy"):
            return StrategyOpResult(False, "MLEngine.add_strategy 未实现",
                                    data={"http_status": 501})
        try:
            self.engine.add_strategy(req.strategy_name, req.class_name)
        except Exception as exc:  # pragma: no cover
            return StrategyOpResult(False, f"创建策略异常: {exc}")
        return StrategyOpResult(True, "added",
                                data={"strategy_name": req.strategy_name})

    # ---- ML 专属读取方法 ----

    def get_latest_metrics(self, name: str) -> Optional[Dict[str, Any]]:
        if hasattr(self.engine, "get_latest_metrics"):
            return self.engine.get_latest_metrics(name)
        return None

    def get_metrics_history(self, name: str, days: int = 30) -> List[Dict[str, Any]]:
        if hasattr(self.engine, "get_metrics_history"):
            return self.engine.get_metrics_history(name, n=days)
        return []

    def get_prediction_summary(self, name: str) -> Optional[Dict[str, Any]]:
        """最新一日预测 summary: metrics + topk.

        metrics/histogram 来自 MetricsCache, topk 从磁盘最新 selections.parquet 读取.
        """
        metrics = self.get_latest_metrics(name)
        if not metrics:
            return None

        topk: List[Dict[str, Any]] = []
        strat = getattr(self.engine, "strategies", {}).get(name)
        if strat is not None:
            topk = self._load_latest_topk(strat)

        return {
            "strategy": name,
            "trade_date": metrics.get("trade_date"),
            "model_run_id": metrics.get("model_run_id"),
            "n_symbols": metrics.get("n_predictions", 0),
            "score_histogram": metrics.get("score_histogram", []),
            "pred_mean": metrics.get("pred_mean"),
            "pred_std": metrics.get("pred_std"),
            "topk": topk,
        }

    @staticmethod
    def _load_latest_topk(strat: Any) -> List[Dict[str, Any]]:
        """读 {output_root}/{name}/ 下最新一天的 selections.parquet."""
        import pandas as pd
        from pathlib import Path

        output_root = getattr(strat, "output_root", None)
        strat_name = getattr(strat, "strategy_name", None)
        if not output_root or not strat_name:
            return []
        strat_dir = Path(output_root) / strat_name
        if not strat_dir.exists():
            return []
        day_dirs = sorted(
            (d for d in strat_dir.iterdir() if d.is_dir() and d.name.isdigit() and len(d.name) == 8),
            reverse=True,
        )
        for day_dir in day_dirs:
            p = day_dir / "selections.parquet"
            if not p.exists():
                continue
            try:
                df = pd.read_parquet(p)
            except Exception:
                continue
            return [
                {
                    "rank": int(r.get("rank", i + 1)) if pd.notna(r.get("rank", i + 1)) else i + 1,
                    "instrument": str(r.get("instrument", "")),
                    "name": (str(r.get("name")) if pd.notna(r.get("name")) else None),
                    "score": float(r.get("score")) if pd.notna(r.get("score")) else None,
                    "weight": float(r.get("weight")) if pd.notna(r.get("weight")) else None,
                }
                for i, (_, r) in enumerate(df.iterrows())
            ]
        return []

    # ---- 历史预测 (Phase 3.2) ----

    def list_prediction_dates(self, name: str) -> List[str]:
        """扫 {output_root}/{name}/ 下 YYYYMMDD 子目录, 返回升序日期列表 (YYYY-MM-DD).

        仅返回同时含 ``metrics.json`` 和 ``selections.parquet`` 的天 — 缺一个
        说明当天 pipeline 异常, 暴露给前端 DatePicker 没意义.
        """
        from pathlib import Path

        strat = getattr(self.engine, "strategies", {}).get(name)
        if strat is None:
            return []
        output_root = getattr(strat, "output_root", None)
        if not output_root:
            return []
        strat_dir = Path(output_root) / name
        if not strat_dir.exists():
            return []
        out: List[str] = []
        for d in strat_dir.iterdir():
            if not d.is_dir() or not d.name.isdigit() or len(d.name) != 8:
                continue
            if not (d / "metrics.json").exists():
                continue
            if not (d / "selections.parquet").exists():
                continue
            out.append(f"{d.name[:4]}-{d.name[4:6]}-{d.name[6:8]}")
        out.sort()
        return out

    def get_prediction_summary_by_date(
        self, name: str, yyyymmdd: str,
    ) -> Optional[Dict[str, Any]]:
        """按日组装预测 summary — 与 get_prediction_summary (latest) 同构,
        但读 ``{output_root}/{name}/{yyyymmdd}/`` 下的 metrics.json + selections.parquet
        而不是 MetricsCache. 给 mlearnweb historical_predictions_sync 用.
        """
        import json
        import pandas as pd
        from pathlib import Path

        if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
            return None
        strat = getattr(self.engine, "strategies", {}).get(name)
        if strat is None:
            return None
        output_root = getattr(strat, "output_root", None)
        if not output_root:
            return None
        day_dir = Path(output_root) / name / yyyymmdd
        if not day_dir.is_dir():
            return None

        metrics_path = day_dir / "metrics.json"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                metrics = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(metrics, dict):
            return None

        topk: List[Dict[str, Any]] = []
        sel_path = day_dir / "selections.parquet"
        if sel_path.exists():
            try:
                df = pd.read_parquet(sel_path)
                topk = [
                    {
                        "rank": int(r.get("rank", i + 1)) if pd.notna(r.get("rank", i + 1)) else i + 1,
                        "instrument": str(r.get("instrument", "")),
                        "name": (str(r.get("name")) if pd.notna(r.get("name")) else None),
                        "score": float(r.get("score")) if pd.notna(r.get("score")) else None,
                        "weight": float(r.get("weight")) if pd.notna(r.get("weight")) else None,
                    }
                    for i, (_, r) in enumerate(df.iterrows())
                ]
            except Exception:
                topk = []

        return {
            "strategy": name,
            "trade_date": metrics.get("trade_date"),
            "model_run_id": metrics.get("model_run_id"),
            "n_symbols": metrics.get("n_predictions", 0),
            "score_histogram": metrics.get("score_histogram", []),
            "pred_mean": metrics.get("pred_mean"),
            "pred_std": metrics.get("pred_std"),
            "topk": topk,
        }

    def get_health(self) -> Dict[str, Any]:
        """ML 专属健康检查: 在基类通用字段基础上追加 ML 运行状态字段."""
        strategies = getattr(self.engine, "strategies", {})
        result: List[Dict[str, Any]] = []
        for name, obj in strategies.items():
            result.append({
                "name": name,
                "inited": bool(getattr(obj, "inited", False)),
                "trading": bool(getattr(obj, "trading", False)),
                "last_run_date": getattr(obj, "last_run_date", ""),
                "last_status": getattr(obj, "last_status", ""),
                "last_error": getattr(obj, "last_error", ""),
                "last_model_run_id": getattr(obj, "last_model_run_id", ""),
                "last_n_pred": getattr(obj, "last_n_pred", 0),
                "last_duration_ms": getattr(obj, "last_duration_ms", 0),
            })
        return {"strategies": result}


# ---------------------------------------------------------------------------
# 注册表与构建函数
# ---------------------------------------------------------------------------


ADAPTER_REGISTRY: Dict[str, Type[StrategyEngineAdapter]] = {
    CtaStrategyAdapter.app_name: CtaStrategyAdapter,
    SignalStrategyPlusAdapter.app_name: SignalStrategyPlusAdapter,
    LegacySignalStrategyAdapter.app_name: LegacySignalStrategyAdapter,
    MLStrategyAdapter.app_name: MLStrategyAdapter,
}


def register_adapter(cls: Type[StrategyEngineAdapter]) -> Type[StrategyEngineAdapter]:
    """装饰器形式注册自定义适配器。"""
    ADAPTER_REGISTRY[cls.app_name] = cls
    return cls


def build_adapters(main_engine: Any) -> Dict[str, StrategyEngineAdapter]:
    """遍历 ``main_engine.engines``, 为已知的策略引擎挂上适配器。"""
    adapters: Dict[str, StrategyEngineAdapter] = {}
    engines = getattr(main_engine, "engines", {}) or {}
    for app_name, adapter_cls in ADAPTER_REGISTRY.items():
        engine = engines.get(app_name)
        if engine is None:
            continue
        try:
            adapters[app_name] = adapter_cls(engine)
        except Exception:  # pragma: no cover - defensive
            continue
    return adapters
