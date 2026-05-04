"""[P2-1 V2] 零实盘环境下的 QmtGateway 替身.

挂着 'QMT' 名字 (命名约定 → live), 内部撮合复用 QmtSimGateway 的 SimulationCounter
+ MD + TD. 仅用于双轨架构验证, **绝不应**进入生产部署:

  ✗ 部署机不应安装 tests/ 目录 (deploy/install_services.ps1 必跳过)
  ✓ 研发机跑 V2 (test_dual_track_with_fake_live) 用此替身配合 QMT_SIM_* sim
    gateway 验证: 多 gateway 路由架构、命名 validator live/sim 各自分支、
    signal_source_strategy 同步上游

为何不直接用真 QmtGateway: 真 QmtGateway 需要 miniqmt 客户端 + 券商账户 +
仅交易时段可用; 验证多 gateway **架构**层面 (R1 EventEngine 隔离 / R2 send_order
路由 / R3 on_order/on_trade 路由) 不需要真券商交互, 用 sim 撮合内核足够.

V3 (真券商仿真账户) 留 TODO 待下一交易日盘中, 在 docs/deployment_a1_p21_plan.md
§三.2 V3 章节.
"""
from __future__ import annotations

from typing import Any

from vnpy.event import EventEngine
from vnpy.trader.gateway import BaseGateway

from vnpy_qmt_sim.gateway import QmtSimGateway


class FakeQmtGateway(QmtSimGateway):
    """伪装 QmtGateway 接口的模拟柜台.

    对外 ``default_name = "QMT"`` 让 vnpy_common.naming.classify_gateway 识别为
    live; 内部继承 QmtSimGateway 撮合/持久化/MD 全部, 没有真实下单风险.

    与 QmtSimGateway 的差异 (仅命名层):
      * default_name 改 "QMT"
      * __init__ 跳过 validate_gateway_name(expected_class='sim') 校验
        (允许 gateway_name='QMT' 即合规 live 命名)
      * 命名 validator 启动期会按 expected_class='live' 校验本实例 (在
        run_ml_headless._validate_startup_config 里 kind='fake_live' 走 live 分支)
    """

    default_name = "QMT"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "QMT"):
        # 跳过 QmtSimGateway.__init__ 里的 validate_gateway_name(expected_class="sim"),
        # 直接调用其余初始化逻辑. 不能直接 super().__init__(event_engine, gateway_name)
        # 因为那会触发 sim 校验 raise.
        BaseGateway.__init__(self, event_engine, gateway_name)

        # 复制 QmtSimGateway.__init__ 后半段
        from vnpy_qmt_sim.gateway import QmtSimMd, QmtSimTd
        self.md = QmtSimMd(self)
        self.td = QmtSimTd(self)
        self._timer_count = 0
        self._order_timeout_interval = 1
        self.connected = False
        self._last_seen_date = None
        self._auto_settle_enabled = True
