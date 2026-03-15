# 修复与文档编写计划

## 1. 重构策略添加逻辑 (移除 `strategy_name` 和 `setting` 入参)

### 1.1 修改 `vnpy_signal_strategy/template.py`
- 在 `SignalTemplate` 类中增加类属性 `strategy_name: str = ""` 作为默认策略名。
- 修改 `__init__` 方法的签名，仅保留 `signal_engine` 入参，移除 `strategy_name` 和 `setting`。
- 在 `__init__` 内部，通过 `self.strategy_name = getattr(self.__class__, "strategy_name", self.__class__.__name__)` 自动获取策略名。

### 1.2 修改 `vnpy_signal_strategy/mysql_signal_strategy.py`
- 修改 `MySQLSignalStrategy.__init__` 签名，仅保留 `signal_engine` 入参。
- 在 `__init__` 内部调用 `self.load_external_setting()` 直接从外部 JSON 文件（如 `mysql_signal_setting.json`）加载配置。

### 1.3 修改 `vnpy_signal_strategy/engine.py`
- 修改 `add_strategy` 方法的签名，仅接受 `strategy_class: Union[str, Type[SignalTemplate]]`。
- 移除 `add_strategy` 中保存 `strategy_setting.json` 的相关代码，因为策略将自行从自己的 JSON 配置文件中读取设定。
- 移除 `load_strategy_setting`、`save_strategy_setting` 和 `edit_strategy` 方法及其相关变量，完全摒弃硬编码和引擎层的配置管理。

### 1.4 修改 `vnpy_signal_strategy/ui/widget.py`
- 修改 `add_strategy` 方法，不再弹出 `SettingEditor` 弹窗输入配置，而是直接调用 `self.signal_engine.add_strategy(class_name)` 添加策略。
- 从 `SignalStrategyManager` 中移除“编辑”按钮及相关逻辑，强制用户只能通过修改 JSON 文件并重新加载来更改配置。

### 1.5 修改 `run_sim.py`
- 更新 `main()` 函数中手动添加策略的调用方式，适配新的 `add_strategy(MultiStrategySignalStrategy)` 签名。

---

## 2. 编写 A 股模拟交易逻辑深度 Review 文档

### 2.1 目标与语言
- 输出文件：`docs/a_share_sim_logic.md`。
- 语言：**全中文**。
- 内容深度：极其详细，结合 A 股交易特点。

### 2.2 文档内容结构规划
1. **A股实盘交易核心规则**
   - 交易时间（含集合竞价、连续竞价）。
   - T+1 机制与持仓可用数量（`yd_volume` 与 `volume` 的区别）。
   - 涨跌停板限制。
   - 报单限制（买入 100 整数倍，卖出可零股等）。
2. **`vnpy_signal_strategy` 策略层发单逻辑分析**
   - 信号轮询、仓位计算机制。
   - 对接 A 股时的缺陷分析（如缺少对未成交挂单 frozen 数量的处理、非交易时间的拦截）。
3. **`vnpy_qmt_sim` 模拟柜台撮合逻辑分析**
   - **报单 (Order Placement)**：校验资金与可用持仓。
   - **部分成交 (Partial Fill)**：如何模拟以及资金/持仓的部分解冻机制。
   - **撤单 (Cancel Order)**：解冻逻辑。
   - **拒单 (Reject Order)**：由于资金不足、越权、或 T+1 校验失败导致的拒单。
4. **流程图与时序图 (Mermaid)**
   - 发单与撮合完整生命周期时序图。
   - 报单状态流转状态机（Submitting -> NotTraded -> PartTraded / AllTraded / Cancelled）。
   - 持仓变动与 T+1 计算流程图。

---

通过上述步骤，彻底解耦 UI 与策略配置，满足您的第一点需求；并提供一份专业、深度的中文技术文档，满足您的第二点需求。