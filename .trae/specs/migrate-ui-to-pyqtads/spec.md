# PyQtAds Dock 体系迁移 Spec

## Why
当前前端基于 `QMainWindow + QDockWidget`，在复杂停靠/浮动/布局管理能力上有限。根据现有评估文档，计划迁移到 PyQtAds 以获得更强的 Dock 管理能力，同时尽量保持现有布局与使用习惯不变。

## What Changes
- 引入 PyQtAds（与当前 PySide6 版本匹配的 Python 绑定）并完成依赖集成。
- 重构主窗口 Dock 管理：从 `QDockWidget` 迁移为 PyQtAds 的 DockManager/DockWidget 体系。
- 重新定义并注册 `vnpy/trader/ui` 下主界面 Dock 窗体（交易/行情/委托/活动/成交/日志/资金/持仓）的创建、停靠区域与 Tab 关系，保持与现有布局一致。
- 实现布局持久化与回退策略：
  - 支持 PyQtAds 布局状态保存/恢复；
  - **兼容迁移**：无法读取旧布局时自动回退到默认布局。
- 增加一个运行时开关（例如环境变量或配置项）用于选择“原生 Dock”与“PyQtAds Dock”，便于灰度与回滚。  
- **BREAKING**：历史用户保存的 `QMainWindow.saveState/restoreState` 布局可能无法直接复用，需明确迁移/回退行为。
- 在开始迁移前执行 Git 备份：提交当前有效代码与文档，并在新分支上实施迁移（用户已提出该需求）。

## Impact
- Affected specs: 主界面 Dock 布局、布局持久化/恢复、主题样式覆盖范围（可能需补充 QSS 以覆盖 PyQtAds 标题栏/标签）。
- Affected code:
  - `vnpy/trader/ui/mainwindow.py`（Dock 创建/布局/保存恢复）
  - `vnpy/trader/ui/qt.py`（全局样式、可选补充 PyQtAds 样式适配点）
  - `pyproject.toml`（新增依赖）
  - 可能新增：`vnpy/trader/ui/dockhost.py`（Dock 适配层，隔离 PyQtAds API）
  - 可能新增：`docs/ui_pyqtads_migration.md`（迁移说明与回滚方式）

## ADDED Requirements

### Requirement: Git 备份与分支迁移
系统 SHALL 在进行 PyQtAds 迁移前，允许先将当前仓库有效代码与文档提交为一个可回溯的基线，并在新分支上开展迁移开发。

#### Scenario: 迁移前备份
- **WHEN** 开始执行迁移任务
- **THEN** 工作树变更被提交为基线 commit
- **AND** 创建/切换到新的迁移分支进行后续改动

### Requirement: PyQtAds Dock 布局等价
系统 SHALL 在 PyQtAds 模式下提供与当前主界面等价的 Dock 组成与布局关系（停靠区域、Tab 分组、默认尺寸体验尽量一致）。

#### Scenario: 默认布局一致性
- **WHEN** 用户首次启动 PyQtAds 模式（无可用布局存档）
- **THEN** Dock 窗口集合与默认停靠区域与现状一致
- **AND** 关键 Tab 关系（例如 活动/委托 Tabify）保持一致或提供等价分组

### Requirement: 布局持久化与回退
系统 SHALL 保存并恢复 PyQtAds 布局状态，并在布局数据无效/缺失时回退到默认布局，避免启动失败。

#### Scenario: 恢复失败回退
- **WHEN** 布局恢复失败或数据不兼容
- **THEN** 系统回退到默认布局
- **AND** 给出一次性日志提示（不影响使用）

## MODIFIED Requirements

### Requirement: 主窗口 Dock 管理
主窗口 SHALL 不再依赖 `QDockWidget` 作为唯一 Dock 机制；在启用 PyQtAds 模式时，Dock 的创建、停靠与布局管理由 PyQtAds 完成，并保持现有业务 Widget 复用。

## REMOVED Requirements

### Requirement: 旧布局状态完全兼容
**Reason**: PyQtAds 与 `QMainWindow.saveState/restoreState` 状态格式不同，无法保证 100% 兼容。
**Migration**: 提供模式开关/回退默认布局/必要时保留原生 Dock 模式作为兼容路径。

