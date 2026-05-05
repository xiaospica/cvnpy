"""IC backfill 端到端集成测试 (Phase 1 验收).

模拟完整数据流:
  1. 策略 bundle_dir 含真实 task.json (label = "Ref($close, -N) / Ref($close, -1) - 1")
  2. 磁盘上有历史 metrics.json (IC=null, 等待 backfill)
  3. MLEngine._trigger_ic_backfill 触发
     - _resolve_ic_forward_window 从 task.json 解析正确的 N
     - IcBackfillService 起子进程 (本测试 mock 之, 模拟"subprocess 写 IC 到磁盘")
     - on_complete 回调触发 reload_history_from_disk
  4. MetricsCache 含被 backfill 后的 IC
  5. engine.get_metrics_history (=webtrader 读取路径) 返回的数据 IC 非空

不跑真子进程 (需要 qlib + 真模型 bundle, 太重). 用 monkeypatch 让
IcBackfillService._invoke_subprocess 直接写 fake metrics.json 到磁盘.

Run:
    F:/Program_Home/vnpy/python.exe -m pytest vnpy_ml_strategy/test/test_ic_backfill_e2e.py -v
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vnpy_ml_strategy.monitoring.cache import MetricsCache
from vnpy_ml_strategy.services.ic_backfill import IcBackfillResult


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _write_task_json(bundle_dir: Path, forward_days: int) -> None:
    """Write a minimal but realistic task.json that ic_forward_window can parse."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "dataset": {
            "kwargs": {
                "handler": {
                    "kwargs": {
                        "label": [f"Ref($close, -{forward_days}) / Ref($close, -1) - 1"],
                    }
                },
                "segments": {
                    "train": ["2018-01-01", "2022-12-31"],
                    "test":  ["2024-01-01", "2024-04-30"],
                },
            }
        }
    }
    (bundle_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")


def _write_metrics(output_root: Path, strategy: str, day: date, payload: dict) -> Path:
    day_dir = output_root / strategy / day.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / "metrics.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


def _make_fake_strategy(
    bundle_dir: str,
    output_root: str,
    *,
    strategy_name: str = "csi300_v1",
    provider_uri: str = "/fake/qlib_data_bin",
    inference_python: str = "/fake/python.exe",
    ic_backfill_scan_days: int = 30,
):
    """构造最小策略实例 — 不继承 MLStrategyTemplate (避免 ABC 抽象方法), 用 SimpleNamespace
    + 把 ic_forward_window 模拟成属性 (从 task.json 解析).

    实际生产中走的是 MLStrategyTemplate.ic_forward_window property; 这里用
    模拟版避免引入完整 vnpy 引擎依赖, 但解析逻辑等价 (同一个文件解析).
    """
    # 关键: 让 strat 有真的 ic_forward_window property 行为. 直接 import
    # MLStrategyTemplate 的 property 函数, 把它绑到一个 stub 上.
    from vnpy_ml_strategy.template import MLStrategyTemplate

    class _Strat:
        pass

    s = _Strat()
    s.bundle_dir = bundle_dir
    s.output_root = output_root
    s.provider_uri = provider_uri
    s.inference_python = inference_python
    s.strategy_name = strategy_name
    s.ic_backfill_scan_days = ic_backfill_scan_days
    s._cached_ic_forward_window = None
    # 把 property descriptor 挂到 _Strat 类上, 这样 s.ic_forward_window 走真逻辑
    _Strat.ic_forward_window = MLStrategyTemplate.ic_forward_window
    _Strat._IC_FORWARD_LABEL_PATTERN = MLStrategyTemplate._IC_FORWARD_LABEL_PATTERN
    return s


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------


class TestIcBackfillE2EFlow:
    """从 _trigger_ic_backfill 到 cache 含 IC 的完整链路测试."""

    def _setup_disk(self, tmp_path: Path, forward_days: int):
        bundle = tmp_path / "bundle"
        output_root = tmp_path / "ml_output"
        _write_task_json(bundle, forward_days)
        # 写 5 天历史 metrics.json, IC 全 null (等待 backfill)
        today = date.today()
        for i in range(5):
            d = today - timedelta(days=i + 1)
            _write_metrics(output_root, "csi300_v1", d, {
                "ic": None,
                "rank_ic": None,
                "n_predictions": 300 + i,
                "pred_mean": 0.001 + i * 0.0001,
            })
        return bundle, output_root

    def _simulate_subprocess_writes_ic(
        self,
        output_root: Path,
        strategy: str,
        forward_window: int,
    ):
        """模拟 run_ic_backfill 子进程: 把 'IC 已算好' 写入磁盘 metrics.json.

        实际子进程从 qlib bin 拉 close 算 IC, 这里我们直接写 fake 真值,
        让 reload_history_from_disk 读到的有 IC. 关键是: 写入的 forward_window
        值就是 forward_window 参数 — 这样可以验证 _resolve_ic_forward_window
        传到子进程的值是对的.
        """
        from datetime import date as _date
        today = _date.today()
        for i in range(5):
            d = today - timedelta(days=i + 1)
            f = output_root / strategy / d.strftime("%Y%m%d") / "metrics.json"
            if f.exists():
                payload = json.loads(f.read_text(encoding="utf-8"))
                # 给每天写个独特但已知的 IC 值, 含 forward_window 元信息
                payload["ic"] = 0.01 + i * 0.005
                payload["rank_ic"] = 0.02 + i * 0.005
                payload["forward_window"] = forward_window
                f.write_text(json.dumps(payload), encoding="utf-8")

    @pytest.mark.parametrize("forward_days", [11, 2, 20])
    def test_e2e_run_ic_backfill_now_resolves_correct_forward(
        self, tmp_path: Path, forward_days: int,
    ):
        """端到端: run_ic_backfill_now 用从 task.json 解析的 forward_days,
        子进程结束后 cache 被刷新, 含正确 IC 值."""
        from vnpy_ml_strategy.engine import MLEngine
        from vnpy_ml_strategy.services import ic_backfill as ic_backfill_module

        bundle, output_root = self._setup_disk(tmp_path, forward_days)
        strat = _make_fake_strategy(
            bundle_dir=str(bundle),
            output_root=str(output_root),
        )

        # 构造极简 engine 替身, 只让 _resolve_ic_forward_window /
        # _on_ic_backfill_complete / run_ic_backfill_now 走真逻辑
        class _Engine:
            strategies = {"csi300_v1": strat}
            _metrics_cache = MetricsCache(max_history_days=30)
            _ic_backfill_services = {}
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
            _on_ic_backfill_complete = MLEngine._on_ic_backfill_complete
            _trigger_ic_backfill = MLEngine._trigger_ic_backfill
            run_ic_backfill_now = MLEngine.run_ic_backfill_now

        engine = _Engine()

        # 监听子进程的 forward_window 参数 — 验证传对了
        captured_forward_window = []

        def fake_invoke(self):
            # 验证 service 实例的 forward_window 跟 task.json 解析一致
            captured_forward_window.append(self.forward_window)
            # 模拟"子进程把 IC 写盘"
            self_simulate_subprocess_writes_ic(
                Path(self.output_root), self.strategy_name, self.forward_window,
            )
            # 返回成功 result
            return IcBackfillResult(
                success=True, scanned=5, computed=5, raw={"forward_window": self.forward_window},
            )

        # 上面 lambda 拼写错了, 修一下
        outer_self = self
        def fake_invoke_real(self):
            captured_forward_window.append(self.forward_window)
            outer_self._simulate_subprocess_writes_ic(
                Path(self.output_root), self.strategy_name, self.forward_window,
            )
            return IcBackfillResult(
                success=True, scanned=5, computed=5, raw={"forward_window": self.forward_window},
            )

        with patch.object(
            ic_backfill_module.IcBackfillService,
            "_invoke_subprocess",
            fake_invoke_real,
        ):
            result = engine.run_ic_backfill_now("csi300_v1")

        # 验证 1: 子进程拿到了正确 forward_window (从 task.json 解析)
        assert captured_forward_window == [forward_days], (
            f"forward_window 应来自 task.json={forward_days}, 实际 {captured_forward_window}"
        )

        # 验证 2: 子进程返回 success=True, computed=5
        assert result is not None
        assert result.success
        assert result.computed == 5

        # 验证 3: 关键 — MetricsCache 被 reload 了, 含 IC (而非 None)
        history = engine._metrics_cache.get_history("csi300_v1", days=30)
        assert len(history) == 5, f"应 reload 5 天, 实际 {len(history)}"
        # 验证: history 中的 IC 不再是 None, 且就是子进程写盘的值
        ic_values = [h.get("ic") for h in history]
        assert None not in ic_values, f"reload 后 IC 应非 None, 实际 {ic_values}"
        # 验证 forward_window 元信息也被 reload (证明读的是子进程写后的版本, 不是 stale)
        forward_in_cache = [h.get("forward_window") for h in history]
        assert all(fw == forward_days for fw in forward_in_cache), (
            f"cache 中 forward_window 应全 = {forward_days}, 实际 {forward_in_cache}"
        )

    def test_e2e_skips_when_label_unparseable(self, tmp_path: Path):
        """label 表达式不匹配模板时, 整个 backfill skip — 不算错误的 IC."""
        from vnpy_ml_strategy.engine import MLEngine
        from vnpy_ml_strategy.services import ic_backfill as ic_backfill_module

        bundle = tmp_path / "bundle_bad"
        output_root = tmp_path / "ml_output"
        bundle.mkdir(parents=True)
        # 写一个不匹配的 label
        (bundle / "task.json").write_text(
            json.dumps({"dataset": {"kwargs": {"handler": {"kwargs": {
                "label": ["Mean($close, 5)"],  # 非 forward return 模板
            }}}}}),
            encoding="utf-8",
        )
        _write_metrics(output_root, "weird_strat", date.today(), {"ic": None})

        strat = _make_fake_strategy(
            bundle_dir=str(bundle),
            output_root=str(output_root),
            strategy_name="weird_strat",
        )

        class _Engine:
            strategies = {"weird_strat": strat}
            _metrics_cache = MetricsCache(max_history_days=30)
            _ic_backfill_services = {}
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
            _on_ic_backfill_complete = MLEngine._on_ic_backfill_complete
            run_ic_backfill_now = MLEngine.run_ic_backfill_now

        engine = _Engine()

        invoked = []
        def fake_invoke(self):
            invoked.append(True)
            return IcBackfillResult(success=True)

        with patch.object(
            ic_backfill_module.IcBackfillService,
            "_invoke_subprocess",
            fake_invoke,
        ):
            result = engine.run_ic_backfill_now("weird_strat")

        # 关键: result is None, 子进程根本没起 — 防御了"用错误 forward 算 IC"的灾难
        assert result is None
        assert invoked == [], "解析失败时不应起 subprocess"

    def test_e2e_async_path_via_trigger_ic_backfill(self, tmp_path: Path):
        """异步路径 _trigger_ic_backfill: 起后台线程, 验证 cache 最终被刷新."""
        from vnpy_ml_strategy.engine import MLEngine
        from vnpy_ml_strategy.services import ic_backfill as ic_backfill_module
        import time

        bundle, output_root = self._setup_disk(tmp_path, forward_days=11)
        strat = _make_fake_strategy(
            bundle_dir=str(bundle),
            output_root=str(output_root),
        )

        class _Engine:
            strategies = {"csi300_v1": strat}
            _metrics_cache = MetricsCache(max_history_days=30)
            _ic_backfill_services = {}
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
            _on_ic_backfill_complete = MLEngine._on_ic_backfill_complete
            _trigger_ic_backfill = MLEngine._trigger_ic_backfill

        engine = _Engine()

        outer_self = self
        def fake_invoke(self):
            outer_self._simulate_subprocess_writes_ic(
                Path(self.output_root), self.strategy_name, self.forward_window,
            )
            return IcBackfillResult(success=True, scanned=5, computed=5)

        with patch.object(
            ic_backfill_module.IcBackfillService,
            "_invoke_subprocess",
            fake_invoke,
        ):
            engine._trigger_ic_backfill("csi300_v1", str(output_root))
            # 等后台线程跑完 (debounce + worker), 最多 5s
            for _ in range(50):
                history = engine._metrics_cache.get_history("csi300_v1")
                if history and any(h.get("ic") is not None for h in history):
                    break
                time.sleep(0.1)

        history = engine._metrics_cache.get_history("csi300_v1", days=30)
        ic_values = [h.get("ic") for h in history]
        assert any(v is not None for v in ic_values), (
            f"异步 backfill 完成后 cache 应含 IC, 实际 {ic_values}"
        )

    def test_e2e_failed_subprocess_does_not_corrupt_cache(self, tmp_path: Path):
        """子进程失败时 (success=False) cache 不应被 reload, 避免读到半截写入."""
        from vnpy_ml_strategy.engine import MLEngine
        from vnpy_ml_strategy.services import ic_backfill as ic_backfill_module

        bundle, output_root = self._setup_disk(tmp_path, forward_days=11)
        strat = _make_fake_strategy(
            bundle_dir=str(bundle),
            output_root=str(output_root),
        )

        class _Engine:
            strategies = {"csi300_v1": strat}
            _metrics_cache = MetricsCache(max_history_days=30)
            _ic_backfill_services = {}
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
            _on_ic_backfill_complete = MLEngine._on_ic_backfill_complete
            run_ic_backfill_now = MLEngine.run_ic_backfill_now

        engine = _Engine()

        # 预先填一些 cache 数据 (来自之前 publish_metrics)
        engine._metrics_cache.update("csi300_v1", {"ic": 0.99, "n_predictions": 999})

        def fake_invoke(self):
            return IcBackfillResult(
                success=False, error_message="subprocess crash",
            )

        with patch.object(
            ic_backfill_module.IcBackfillService,
            "_invoke_subprocess",
            fake_invoke,
        ):
            result = engine.run_ic_backfill_now("csi300_v1")

        # 失败 result 返回, 但 cache 不被 reload (旧值 0.99 保留)
        assert result is not None
        assert not result.success
        latest = engine._metrics_cache.get_latest("csi300_v1")
        assert latest is not None
        assert latest["ic"] == 0.99, "失败 backfill 不应改 cache"


class TestE2ESummary:
    """整合验证: Phase 1 三处改动是否都生效."""

    def test_phase1_full_chain(self, tmp_path: Path):
        """Phase 1 总验收 — 一个测试覆盖 3 处关键改动:
        1. cache_loader.py 的 reload_history_from_disk 实际工作
        2. MLStrategyTemplate.ic_forward_window 从 task.json 解析
        3. MLEngine._on_ic_backfill_complete 把磁盘改动 reload 到 cache
        """
        from vnpy_ml_strategy.engine import MLEngine
        from vnpy_ml_strategy.services import ic_backfill as ic_backfill_module

        # 准备: bundle (label = 7 日 forward) + 历史 metrics.json (IC=null)
        bundle = tmp_path / "bundle"
        output_root = tmp_path / "out"
        _write_task_json(bundle, forward_days=7)
        today = date.today()
        for i in range(3):
            _write_metrics(output_root, "test_strat", today - timedelta(days=i + 1), {
                "ic": None, "n_predictions": 100,
            })

        strat = _make_fake_strategy(
            bundle_dir=str(bundle),
            output_root=str(output_root),
            strategy_name="test_strat",
        )

        # ====== 检查点 1: ic_forward_window 解析正确 ======
        assert strat.ic_forward_window == 7

        # ====== 检查点 2: engine wrapper 正确传递 ======
        class _Engine:
            strategies = {"test_strat": strat}
            _metrics_cache = MetricsCache(max_history_days=30)
            _ic_backfill_services = {}
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
            _on_ic_backfill_complete = MLEngine._on_ic_backfill_complete
            run_ic_backfill_now = MLEngine.run_ic_backfill_now

        engine = _Engine()
        assert engine._resolve_ic_forward_window(strat, "test_strat") == 7

        # ====== 检查点 3: 完整链路 ======
        captured_fw = []
        def fake_invoke(self):
            captured_fw.append(self.forward_window)
            # 模拟子进程: 把 3 天 metrics.json 的 IC 字段写入
            for i in range(3):
                d = today - timedelta(days=i + 1)
                f = output_root / "test_strat" / d.strftime("%Y%m%d") / "metrics.json"
                payload = json.loads(f.read_text(encoding="utf-8"))
                payload["ic"] = 0.05 + i * 0.01
                payload["rank_ic"] = 0.07 + i * 0.01
                f.write_text(json.dumps(payload), encoding="utf-8")
            return IcBackfillResult(success=True, scanned=3, computed=3)

        with patch.object(
            ic_backfill_module.IcBackfillService, "_invoke_subprocess", fake_invoke,
        ):
            result = engine.run_ic_backfill_now("test_strat")

        # 子进程拿到了 forward_window=7
        assert captured_fw == [7]
        # cache 被 reload 后含 IC
        history = engine._metrics_cache.get_history("test_strat", days=30)
        ic_values = sorted([h.get("ic") for h in history if h.get("ic") is not None])
        assert ic_values == pytest.approx([0.05, 0.06, 0.07]), f"实际 cache IC: {ic_values}"
        # rank_ic 也应该被 reload 进来
        rank_ic_values = sorted([h.get("rank_ic") for h in history if h.get("rank_ic") is not None])
        assert rank_ic_values == pytest.approx([0.07, 0.08, 0.09]), f"实际 rank_ic: {rank_ic_values}"

        print("\n✓ Phase 1 全链路验证通过:")
        print(f"  - ic_forward_window 解析: 7 (从 task.json)")
        print(f"  - subprocess 收到的 forward_window: {captured_fw[0]}")
        print(f"  - 子进程改盘后 cache reload: {len(history)} 天 metrics 全部含 IC")
