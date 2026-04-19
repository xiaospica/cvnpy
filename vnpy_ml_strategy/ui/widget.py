"""vnpy_ml_strategy UI widget.

改自 vnpy_signal_strategy_plus.ui.widget, 适配 ML 策略的生命周期与特色操作:

  * 顶栏: class 下拉 + 添加 + 查找 + 全部初始化/启动/停止 + 清空日志
  * 左侧: 每个策略一个 <MLStrategyManagerPlus> 面板 (滚动布局)
      - 按钮: 初始化 / 启动 / 停止 / 移除 / **立即触发 pipeline** (ML 特色)
      - Parameters / Variables 两张 DataMonitor
  * 右侧: LogMonitorPlus 展示 eLog 事件

事件驱动:
  - 消费 EVENT_ML_STRATEGY 刷新策略面板
  - 本 widget 初始化时需要 MLEngine 已加载 + 若干策略类已 register_strategy_class
"""

from typing import Any, Dict

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.ui.widget import BaseMonitor, MsgCell, TimeCell

from ..base import APP_NAME, EVENT_ML_STRATEGY
from ..engine import MLEngine


class MLStrategyManager(QtWidgets.QWidget):
    """ML 策略管理主窗口. vnpy MainWindow 的功能菜单打开的是这个类."""

    # Qt signal 桥接 (EventEngine 在任意线程 put, 通过 signal/slot 回主线程 UI)
    signal_strategy: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.ml_engine: MLEngine = main_engine.get_engine(APP_NAME)

        self.managers: Dict[str, "MLStrategyPanel"] = {}

        self._init_ui()
        self._register_event()
        # CRITICAL: 在更新 UI 前触发 MLEngine.init_engine() 以填充 strategy_classes
        # 字典(通过 _autoload_strategy_classes 注册 QlibMLStrategy 等). vnpy
        # MainEngine.add_app 只调 Engine.__init__, 不调 init_engine, 因此 UI
        # widget 打开时必须显式触发一次. 参考 signal_strategy_plus/ui/widget.py.
        # init_engine 是幂等的, 重复调用(CLI headless 也会调)不会出问题.
        self.ml_engine.init_engine()
        self._update_class_combo()
        self._snapshot_existing_strategies()

    # -----------------------------------------------------------------
    # 初始化
    # -----------------------------------------------------------------

    def _init_ui(self) -> None:
        self.setWindowTitle("ML 策略管理")

        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.resize(max(int(screen.width() * 0.7), 1400), max(int(screen.height() * 0.7), 900))

        # 顶栏控件
        self.class_combo = QtWidgets.QComboBox()
        self.class_combo.setMinimumWidth(220)

        add_btn = QtWidgets.QPushButton("添加策略")
        add_btn.clicked.connect(self._add_strategy_dialog)

        init_all_btn = QtWidgets.QPushButton("全部初始化")
        init_all_btn.clicked.connect(self._init_all)

        start_all_btn = QtWidgets.QPushButton("全部启动")
        start_all_btn.clicked.connect(self.ml_engine.start_all_strategies)

        stop_all_btn = QtWidgets.QPushButton("全部停止")
        stop_all_btn.clicked.connect(self.ml_engine.stop_all_strategies)

        clear_log_btn = QtWidgets.QPushButton("清空日志")
        clear_log_btn.clicked.connect(self._clear_log)

        self.strategy_combo = QtWidgets.QComboBox()
        self.strategy_combo.setMinimumWidth(220)
        find_btn = QtWidgets.QPushButton("定位")
        find_btn.clicked.connect(self._find_strategy)

        # 左: 策略面板滚动区
        self.scroll_layout = QtWidgets.QVBoxLayout()
        self.scroll_layout.addStretch()

        scroll_inner = QtWidgets.QWidget()
        scroll_inner.setLayout(self.scroll_layout)
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(scroll_inner)

        # 右: 日志
        self.log_monitor = LogMonitor(self.main_engine, self.event_engine)

        # 布局
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.class_combo)
        top.addWidget(add_btn)
        top.addStretch()
        top.addWidget(self.strategy_combo)
        top.addWidget(find_btn)
        top.addStretch()
        top.addWidget(init_all_btn)
        top.addWidget(start_all_btn)
        top.addWidget(stop_all_btn)
        top.addWidget(clear_log_btn)

        grid = QtWidgets.QGridLayout()
        grid.addWidget(self.scroll_area, 0, 0)
        grid.addWidget(self.log_monitor, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(top)
        vbox.addLayout(grid)
        self.setLayout(vbox)

    def _register_event(self) -> None:
        self.signal_strategy.connect(self._on_strategy_event)
        self.event_engine.register(EVENT_ML_STRATEGY, self.signal_strategy.emit)

    def _snapshot_existing_strategies(self) -> None:
        """Widget 打开时, 已有策略通过 put_strategy_event 通告自己."""
        for strat in self.ml_engine.strategies.values():
            self.ml_engine.put_strategy_event(strat)

    def _update_class_combo(self) -> None:
        self.class_combo.clear()
        self.class_combo.addItems(self.ml_engine.get_all_strategy_class_names())

    def _update_strategy_combo(self) -> None:
        names = sorted(self.managers.keys())
        self.strategy_combo.clear()
        self.strategy_combo.addItems(names)

    # -----------------------------------------------------------------
    # 事件 → UI 更新
    # -----------------------------------------------------------------

    def _on_strategy_event(self, event: Event) -> None:
        data: Dict[str, Any] = event.data
        name: str = data["strategy_name"]
        if name in self.managers:
            self.managers[name].update_data(data)
        else:
            panel = MLStrategyPanel(self, self.ml_engine, data)
            self.scroll_layout.insertWidget(0, panel)
            self.managers[name] = panel
            self._update_strategy_combo()

    # -----------------------------------------------------------------
    # 按钮槽
    # -----------------------------------------------------------------

    def _add_strategy_dialog(self) -> None:
        class_name = self.class_combo.currentText().strip()
        if not class_name:
            return

        strategy_name, ok = QtWidgets.QInputDialog.getText(
            self, "新增 ML 策略", "策略实例名:", text=f"{class_name}_demo",
        )
        if not ok or not strategy_name.strip():
            return

        try:
            inst = self.ml_engine.add_strategy(class_name, strategy_name.strip())
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "添加失败", str(exc))
            return

        QtWidgets.QMessageBox.information(
            self, "已添加",
            f"策略 {strategy_name} 已创建. 请在面板里点击[编辑参数]配置 "
            f"bundle_dir / gateway 等, 然后[初始化].",
        )

    def _init_all(self) -> None:
        if not self.ml_engine.init_all_strategies():
            QtWidgets.QMessageBox.warning(
                self, "警告", "部分策略初始化失败, 请检查日志 (常见原因: bundle_dir 未配置 / gateway 未连接)",
            )

    def _find_strategy(self) -> None:
        name = self.strategy_combo.currentText()
        if name and name in self.managers:
            self.scroll_area.ensureWidgetVisible(self.managers[name])

    def _clear_log(self) -> None:
        self.log_monitor.setRowCount(0)

    def remove_strategy_panel(self, name: str) -> None:
        panel = self.managers.pop(name, None)
        if panel is not None:
            panel.deleteLater()
            self._update_strategy_combo()

    def show(self) -> None:
        self.showNormal()


class MLStrategyPanel(QtWidgets.QFrame):
    """单策略面板 — 包按钮 + parameters + variables 两张表."""

    def __init__(self, owner: MLStrategyManager, ml_engine: MLEngine, data: dict) -> None:
        super().__init__()
        self.owner = owner
        self.ml_engine = ml_engine
        self.strategy_name: str = data["strategy_name"]
        self._data = data
        self._init_ui()
        self.update_data(data)

    def _init_ui(self) -> None:
        self.setFixedHeight(260)
        self.setFrameShape(self.Shape.Box)
        self.setLineWidth(1)

        self.init_btn = QtWidgets.QPushButton("初始化")
        self.init_btn.clicked.connect(self._init)

        self.start_btn = QtWidgets.QPushButton("启动")
        self.start_btn.clicked.connect(self._start)
        self.start_btn.setEnabled(False)

        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)

        self.trigger_btn = QtWidgets.QPushButton("立即触发 pipeline")
        self.trigger_btn.setStyleSheet("QPushButton { background-color: #d4edda; }")
        self.trigger_btn.clicked.connect(self._trigger_now)

        self.edit_btn = QtWidgets.QPushButton("编辑参数")
        self.edit_btn.clicked.connect(self._edit_params)

        self.remove_btn = QtWidgets.QPushButton("移除")
        self.remove_btn.clicked.connect(self._remove)

        gw = self._data.get("gateway") or "(未设置)"
        label_text = f"{self.strategy_name}  ({self._data['class_name']} - [{gw}])"
        self.label = QtWidgets.QLabel(label_text)
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("font-weight: bold; font-size: 13px;")

        self.parameters_monitor = DataMonitor(self._data.get("parameters") or {})
        self.variables_monitor = DataMonitor(self._data.get("variables") or {})

        btn_row = QtWidgets.QHBoxLayout()
        for b in (self.init_btn, self.start_btn, self.stop_btn, self.trigger_btn, self.edit_btn, self.remove_btn):
            btn_row.addWidget(b)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.label)
        vbox.addLayout(btn_row)
        vbox.addWidget(self.parameters_monitor)
        vbox.addWidget(self.variables_monitor)
        self.setLayout(vbox)

    # UI refresh
    def update_data(self, data: dict) -> None:
        self._data = data
        self.parameters_monitor.update_data(data.get("parameters") or {})
        self.variables_monitor.update_data(data.get("variables") or {})
        gw = data.get("gateway") or "(未设置)"
        self.label.setText(f"{self.strategy_name}  ({data['class_name']} - [{gw}])")

        inited = bool(data.get("inited"))
        trading = bool(data.get("trading"))

        self.init_btn.setEnabled(not inited)
        if inited and not trading:
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.remove_btn.setEnabled(True)
        elif inited and trading:
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.remove_btn.setEnabled(False)
        else:
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.remove_btn.setEnabled(True)

    # Button slots
    def _init(self) -> None:
        if not self.ml_engine.init_strategy(self.strategy_name):
            QtWidgets.QMessageBox.warning(
                self, "初始化失败",
                "初始化失败. 常见原因:\n"
                "  · bundle_dir 未配置或不存在\n"
                "  · bundle 缺少 params.pkl / task.json\n"
                "  · provider_uri 未配置 (calendar 失败)\n"
                "请在日志窗口查看详情.",
            )

    def _start(self) -> None:
        self.ml_engine.start_strategy(self.strategy_name)

    def _stop(self) -> None:
        self.ml_engine.stop_strategy(self.strategy_name)

    def _trigger_now(self) -> None:
        """ML 特色: 不等 trigger_time, 立即跑一次 run_daily_pipeline."""
        strat = self.ml_engine.strategies.get(self.strategy_name)
        if strat is None:
            QtWidgets.QMessageBox.warning(self, "触发失败", "策略实例不存在")
            return
        if not getattr(strat, "inited", False):
            QtWidgets.QMessageBox.warning(self, "触发失败", "请先初始化")
            return

        text = (
            "即将在 APS 后台线程立即跑一次 run_daily_pipeline.\n"
            "过程包含:\n"
            "  1. subprocess 推理 (~60-120s)\n"
            "  2. 若 enable_trading=True 会真实下单\n"
            "\n"
            "确认触发? "
        )
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("确认立即触发 pipeline")
        box.setText(text)
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        ok = self.ml_engine.run_pipeline_now(self.strategy_name)
        if ok:
            QtWidgets.QMessageBox.information(
                self, "已触发",
                "pipeline 已在后台线程启动. 跑完后 variables 会刷新, 右侧日志也会有输出.",
            )
        else:
            QtWidgets.QMessageBox.warning(self, "触发失败", "调度器返回 False, 请看日志")

    def _edit_params(self) -> None:
        strat = self.ml_engine.strategies.get(self.strategy_name)
        if strat is None:
            return
        current = strat.get_parameters() if hasattr(strat, "get_parameters") else {}

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"编辑 {self.strategy_name} 参数")
        dlg.resize(520, 480)
        form = QtWidgets.QFormLayout()
        editors: Dict[str, QtWidgets.QLineEdit] = {}
        for key, val in current.items():
            edit = QtWidgets.QLineEdit(str(val) if val is not None else "")
            editors[key] = edit
            form.addRow(key, edit)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        outer = QtWidgets.QVBoxLayout(dlg)
        outer.addLayout(form)
        outer.addWidget(btns)

        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        setting: Dict[str, Any] = {}
        for key, edit in editors.items():
            raw = edit.text().strip()
            if raw == "":
                continue
            # 类型推断: 按当前值类型回填
            original = current.get(key)
            try:
                if isinstance(original, bool):
                    setting[key] = raw.lower() in ("true", "1", "yes")
                elif isinstance(original, int) and not isinstance(original, bool):
                    setting[key] = int(raw)
                elif isinstance(original, float):
                    setting[key] = float(raw)
                else:
                    setting[key] = raw
            except ValueError:
                setting[key] = raw

        strat.update_setting(setting)
        self.ml_engine.put_strategy_event(strat)

    def _remove(self) -> None:
        if not self.ml_engine.remove_strategy(self.strategy_name):
            QtWidgets.QMessageBox.warning(self, "移除失败", "策略还在运行中, 请先停止")
            return
        self.owner.remove_strategy_panel(self.strategy_name)


class DataMonitor(QtWidgets.QTableWidget):
    """parameters/variables 展示表, 单行多列."""

    def __init__(self, data: dict) -> None:
        super().__init__()
        self._data = dict(data)
        self.cells: dict = {}
        self._init_ui()

    def _init_ui(self) -> None:
        keys = list(self._data.keys())
        self.setColumnCount(len(keys))
        self.setHorizontalHeaderLabels(keys)
        self.setRowCount(1)
        self.verticalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        for col, name in enumerate(keys):
            val = self._data[name]
            item = QtWidgets.QTableWidgetItem(str(val))
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.setItem(0, col, item)
            self.cells[name] = item

    def update_data(self, data: dict) -> None:
        # Schema change: re-init table
        if set(data.keys()) != set(self._data.keys()):
            self._data = dict(data)
            self.cells.clear()
            self.clear()
            self._init_ui()
            return

        for name, val in data.items():
            cell = self.cells.get(name)
            if cell is not None:
                cell.setText(str(val))
        self._data = dict(data)


class LogMonitor(BaseMonitor):
    """eLog 日志监控, 和 signal_strategy_plus 的 LogMonitorPlus 一致模式."""

    event_type: str = "eLog"
    data_key: str = ""
    sorting: bool = False

    headers: dict = {
        "time": {"display": "时间", "cell": TimeCell, "update": False},
        "msg": {"display": "信息", "cell": MsgCell, "update": False},
    }

    def init_ui(self) -> None:
        super().init_ui()
        self.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )

    def insert_new_row(self, data: Any) -> None:
        # 可按需过滤, 当前不过滤任何来源
        super().insert_new_row(data)
