# PyQtAds Dock 迁移说明

## 1. 启用方式

本项目同时保留两套 Dock 后端：
- 默认：Qt 原生 `QMainWindow + QDockWidget`
- 可选：PyQtAds（Qt Advanced Docking System）

启用 PyQtAds 需要两步：

1) 安装可选依赖

```powershell
& 'F:\Program_Home\vnpy\python.exe' -m pip install -e '.[qtads]'
```

2) 启动时选择 Dock 后端

```powershell
$env:VNPY_DOCK_BACKEND='ads'
& 'F:\Program_Home\vnpy\python.exe' run_sim.py
```

如不设置 `VNPY_DOCK_BACKEND` 或设置为非 `ads`，则使用原生 Dock 后端。

## 2. 回滚方式

### 2.1 运行时回滚（推荐）
- 直接取消环境变量：

```powershell
Remove-Item Env:VNPY_DOCK_BACKEND -ErrorAction SilentlyContinue
& 'F:\Program_Home\vnpy\python.exe' run_sim.py
```

### 2.2 依赖回滚
- 如需移除 QtAds 依赖：

```powershell
& 'F:\Program_Home\vnpy\python.exe' -m pip uninstall -y PySide6-QtAds
```

## 3. 布局保存/恢复行为

- Qt 原生 Dock 与 PyQtAds Dock 的布局 state 采用不同的 key 保存，避免互相污染。
- PyQtAds 模式不会尝试加载旧的 Qt 原生布局 state；首次启用时会以默认布局启动，然后生成并保存自己的布局 state。

## 4. 已知限制

- PyQtAds 与 Qt 原生布局的 state 格式不兼容，历史布局无法 1:1 迁移。
- 某些 PyQtAds 版本的 API 细节存在差异，当前实现以 `PySide6-QtAds==4.3.1.4` 为基准做了兼容。

