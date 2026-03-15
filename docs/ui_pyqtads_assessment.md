# vn.py 前端使用 PyQtAds 替换可行性评估

## 1. 评估范围与目标

本评估仅针对可行性，不进行代码替换实现。目标是评估在保持当前 UI 布局与使用习惯基本一致的前提下，是否可以将现有 Dock 体系迁移为 PyQtAds。

## 2. 当前 UI 架构现状

当前主界面基于 Qt 原生 Dock 体系实现：
- 主框架：`QMainWindow`
- 停靠组件：`QDockWidget`
- 布局操作：`addDockWidget`、`tabifyDockWidget`
- 布局持久化：`saveState/restoreState + QSettings`

关键位置：
- `vnpy/trader/ui/mainwindow.py` 的 `init_dock/create_dock/save_window_setting/load_window_setting`
- `vnpy/trader/ui/qt.py` 的应用初始化与全局样式加载

依赖现状：
- 当前依赖是 `PySide6==6.8.2.1`，未集成 PyQtAds。

## 3. 与 PyQtAds 的适配可行性

结论：可以替换，但不是“零改造”。

可复用部分：
- 业务监控部件与功能窗口大多是 `QWidget/QDialog`，不依赖具体 Dock 库，可直接复用。

必须改造部分：
1. 主窗口 Dock 管理层
   - `QDockWidget` 创建、停靠、标签化逻辑需要替换为 PyQtAds 的 DockManager/DockWidget 体系。
2. 布局持久化机制
   - 现有 `QMainWindow.saveState/restoreState` 与 PyQtAds 的布局状态格式不同，不能直接互通。
3. 窗口还原与默认布局
   - 现有“还原窗口”依赖原生状态快照，迁移后需重建默认布局策略。

## 4. 与“保持现有布局一致”的差异风险

1. 布局状态迁移风险（高）
   - 老版本保存的布局数据无法直接复用，可能导致用户历史布局丢失。
2. 交互行为差异（中高）
   - Dock 浮动、拖拽反馈、标签行为、关闭按钮表现可能与现状有细微差异。
3. 主题样式一致性（中）
   - 现有 qdarkstyle 对 PyQtAds 组件的覆盖程度有限，需补充样式规则。
4. 版本兼容风险（中）
   - 需验证 PyQtAds 与当前 PySide6 版本的兼容性与稳定性。

## 5. 成本评估

中等到偏高，核心成本集中在主窗口基础设施改造，而非业务模块改造。

主要工作量来源：
- Dock 适配层实现
- 布局持久化与回退策略
- 交互回归测试
- 样式一致性调优

## 6. 推荐落地路径

建议分阶段推进：

1. PoC 阶段
   - 在单独分支仅替换主窗口 Dock 层，不动业务 widget。
2. 兼容阶段
   - 增加开关：保留原生 Dock 和 PyQtAds 两套路径并行。
3. 迁移阶段
   - 提供布局重置与迁移提示，避免用户感知为“布局异常”。
4. 稳定阶段
   - 完成回归后再切换为默认方案。

## 7. 结论

从技术上看，vn.py 前端可以迁移到 PyQtAds；但若要求“布局保持与现在一致”，需要对主窗口 Dock 管理和布局持久化做专项改造，并配套兼容与回退策略。建议先 PoC，再灰度切换，不建议一次性直接替换。
