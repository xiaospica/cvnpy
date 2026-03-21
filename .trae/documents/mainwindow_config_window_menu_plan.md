# 主窗口配置路径与窗口菜单需求实现计划

## 一、需求拆解与目标

### 1) 修改 QSettings 保存路径
- 目标：将主窗口布局与几何状态保存到工程根目录 `config/myapp.ini`，而非默认注册表或系统配置路径。
- 行为一致性：保存与恢复均使用该配置文件，确保版本库可追踪、可复制。

### 2) 主 UI 工具栏新增“窗口”菜单
- 目标：在工具栏（或菜单栏）增加“窗口”下拉菜单，动态列出所有 Dock 窗体。
- 交互要求：
  - 若用户通过 Dock 右上角关闭按钮隐藏了窗体，仍可通过该菜单重新打开。
  - 菜单项可勾选（checkable）以反映当前可见状态。
- 兼容性：同时支持原生 QDockWidget 与 PyQtAds 两种后端，抽象层统一处理。

---

## 二、实施步骤（代码级）

### 步骤 1：统一 QSettings 文件路径
1. 在 `vnpy/trader/ui/mainwindow.py` 中新增/修改：
   - 类级别常量 `SETTINGS_FILE = Path.cwd() / "config" / "myapp.ini"`
   - 构造函数确保目录存在：`SETTINGS_FILE.parent.mkdir(exist_ok=True)`
   - 所有 `QSettings` 实例化改为：
     ```python
     settings = QtCore.QSettings(str(SETTINGS_FILE), QtCore.QSettings.IniFormat)
     ```
   - 保存/恢复函数（`save_window_setting/load_window_setting`）同步使用该实例。
2. 将 `config/myapp.ini` 加入 `.gitignore`，但提供 `config/myapp.ini.example` 作为模板。

### 步骤 2：抽象 Dock 可见性接口
1. 在 `vnpy/trader/ui/dockhost.py` 的 `DockHost` 抽象类新增：
   - `get_dock_title(dock: Any) -> str`
   - `set_dock_visible(dock: Any, visible: bool) -> None`
   - `is_dock_visible(dock: Any) -> bool`
   - `iter_docks() -> Iterator[Any]`  # 遍历当前宿主所有 dock
2. 分别在 `QtDockHost` 与 `AdsDockHost` 实现上述方法：
   - Qt：利用 `QDockWidget.setVisible/isVisible` 与 `findChildren(QDockWidget)`
   - Ads：利用 `CDockWidget.toggleView/isClosed` 与 `dockManager.dockWidgets()`

### 步骤 3：主窗口添加“窗口”菜单
1. 在 `MainWindow.init_menu` 新增：
   ```python
   self.window_menu = menubar.addMenu(_("窗口(&W)"))
   self.window_menu.aboutToShow.connect(self._refresh_window_menu)
   ```
2. 实现 `_refresh_window_menu`：
   - 清空菜单，遍历 `self.dock_host.iter_docks()`
   - 为每个 dock 创建 `QAction(text=dock_title, checkable=True)`
   - 根据 `is_dock_visible` 设置勾选状态
   - 连接 `triggered` 到 lambda：`lambda checked, d=dock: self._toggle_dock(d)`
3. 实现 `_toggle_dock(self, dock)`：
   - 若当前不可见，则 `set_dock_visible(dock, True)`
   - 若当前可见，则 `set_dock_visible(dock, False)`

### 步骤 4：保存/恢复时记录 Dock 可见性（可选增强）
- 在 `save_window_setting` 时额外写入一个 `dock_visibility` JSON 字段，记录每个 dock 的可见性
- 在 `load_window_setting` 时读取并应用，确保重启后用户上次关闭的 dock 仍保持关闭状态

### 步骤 5：验证与回归
1. 语法检查：`python -m py_compile vnpy/trader/ui/mainwindow.py vnpy/trader/ui/dockhost.py`
2. 启动测试：
   - 默认/Qt 模式：`python run_sim.py`
   - Ads 模式：`$env:VNPY_DOCK_BACKEND='ads'; python run_sim.py`
3. 手动验证：
   - 关闭任意 Dock → 窗口菜单可重新打开
   - 重启后布局与可见性保持一致
   - 检查 `config/myapp.ini` 文件生成与内容正确性

---

## 三、交付清单

- 代码文件：
  - `vnpy/trader/ui/mainwindow.py`（菜单、保存/恢复路径）
  - `vnpy/trader/ui/dockhost.py`（可见性抽象接口）
- 配置与模板：
  - `.gitignore` 更新（排除 `config/myapp.ini`）
  - `config/myapp.ini.example`
- 文档：
  - 本计划文档（已实现步骤勾选）

---

## 四、注意事项

- 不破坏原有保存字段（geometry、state_qt/state_ads），仅新增可见性字段
- 所有用户可见字符串使用 `_()` 国际化函数
- 敏感信息（如路径）不打印到日志
- 保持与现有评估文档中的 PyQtAds 适配层兼容