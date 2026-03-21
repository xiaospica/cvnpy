# Tasks
- [x] Task 1: 修复 vnpy_signal_strategy_plus 的 App 标识与策略加载
  - [x] 调整 `APP_NAME/display_name` 为不与 `vnpy_ctastrategy` 冲突的名称
  - [x] 修复 `load_strategy_class()` 的默认模块路径为 `vnpy_signal_strategy_plus.strategies`
  - [x] 验证策略扫描可看到 plus 策略类列表

- [x] Task 2: 为策略补齐 timer 驱动与引擎扩展下单接口
  - [x] 引擎注册 `EVENT_TIMER` 并驱动策略 `on_timer`（存在则调用）
  - [x] 增加 “按 vt_symbol 下单” 的公开接口（含 order_type 支持），并纳入订单-策略映射
  - [x] 回测引擎提供兼容的接口/适配（至少单标的回测不报错）

- [x] Task 3: 实现 MySQLSignalStrategyPlus 与 MultiStrategySignalStrategyPlus
  - [x] 将 `mysql_signal_strategy.py` 改造为 CTA 策略基类（继承 `CtaTemplate`）
  - [x] 轮询线程仅做 DB 读取与信号入队，主线程在 `on_timer` 中处理队列并下单
  - [x] 复用/适配 `AutoResubmitMixin`，确保撤单/拒单重挂可用
  - [x] `MultiStrategySignalStrategy` 重命名为 `MultiStrategySignalStrategyPlus`，并修正导入引用

- [x] Task 4: 清理命名冲突与导出名称，补充最小验证
  - [x] 全量检查 `vnpy_signal_strategy_plus` 内对 `vnpy_signal_strategy.*` 的引用并替换为 plus 内实现/适配
  - [x] 检查可被策略扫描加载的类名均不与 `vnpy_signal_strategy` 冲突
  - [x] 增加/运行最小 smoke 验证：导入、策略扫描、回测启动（不要求真实连库）

# Task Dependencies
- Task 2 depends on Task 1 (策略加载路径与 App 标识稳定后再补齐事件与接口)
- Task 3 depends on Task 2（需要 timer 与下单扩展接口）
- Task 4 depends on Task 1-3
