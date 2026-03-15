# Tasks
- [x] Task 1: 建立 Git 基线与迁移分支
  - [x] 确认当前工作树状态并提交“基线备份”commit（仅包含有效代码与文档）
  - [x] 创建并切换到迁移分支（例如 `feature/pyqtads-dock`）

- [ ] Task 2: 引入 PyQtAds 依赖并完成最小可运行集成
  - [ ] 选择与 `PySide6==6.8.2.1` 兼容的 PyQtAds 包/绑定方式并加入依赖
  - [ ] 验证本地可导入与启动（不改业务 UI，仅验证依赖与基本窗口）

- [ ] Task 3: 设计并实现 Dock 适配层（隔离 PyQtAds API）
  - [ ] 定义 `DockHost` 抽象接口（创建 dock、tabify/group、保存/恢复布局）
  - [ ] 实现 `QtDockHost`（现有 QDockWidget 实现）与 `AdsDockHost`（PyQtAds 实现）
  - [ ] 提供运行时开关选择 host（配置项或环境变量）

- [ ] Task 4: 重构主窗口 Dock 创建流程并保持布局一致
  - [ ] 将 `MainWindow.init_dock/create_dock` 改为通过 DockHost 创建
  - [ ] 在 AdsDockHost 下重建当前 8 个 Dock 窗体：交易/行情/委托/活动/成交/日志/资金/持仓
  - [ ] 复刻现有 tab 关系（至少“活动/委托”同组）

- [ ] Task 5: 布局持久化与回退策略实现
  - [ ] Ads 模式下保存/恢复布局状态（与当前 QSettings 机制并存或独立 key）
  - [ ] 恢复失败自动回退默认布局并记录日志

- [ ] Task 6: 样式与主题验证（qdarkstyle + 可选补充 QSS）
  - [ ] 验证 qdarkstyle 对 Ads 标题栏/标签页表现
  - [ ] 必要时补充最小 QSS 覆盖以提升一致性

- [ ] Task 7: 回归验证与文档补充
  - [ ] 启动主界面并检查 Dock 组成、停靠区域、Tab 分组与交互（拖拽/浮动/关闭）
  - [ ] 验证“还原窗口/布局保存恢复”行为
  - [ ] 更新或新增迁移说明文档（启用方式、回退方式、已知限制）

# Task Dependencies
- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 3
- Task 5 depends on Task 4
- Task 6 depends on Task 4
- Task 7 depends on Task 5
