# # 修复 show_about 方法中的导入错误
#
# import sys
# import os
#
# # # 方法1: 禁用深色模式检测（Qt 6.5+）
# # os.environ['QT_QPA_PLATFORM'] = 'windows:darkmode=0'
# #
# # # 方法2: 禁用 Qt 6.7+ 的颜色方案检测
# # os.environ['QT_QPA_PLATFORM'] = 'windows:color_scheme=light'
#
# # ==================== 后端选择配置 ====================
# # 通过环境变量或修改此处来切换后端
# # 可选值: "pyside6" 或 "pyqt5"
# BACKEND = os.environ.get("QT_BACKEND", "pyside6").lower()
# BACKEND = 'pyside6'
#
# # ==================== 后端抽象层 ====================
# if BACKEND == "pyside6":
#     # PySide6 导入
#     from PySide6.QtWidgets import (
#         QApplication, QMainWindow, QWidget, QVBoxLayout,
#         QLabel, QTextEdit, QPushButton, QMenuBar, QToolBar,
#         QStatusBar, QFileDialog, QMessageBox
#     )
#     from PySide6.QtCore import QFile, QTextStream, Qt, QTimer
#     from PySide6.QtGui import QAction
#
#     # PySide6 QtAds 导入
#     try:
#         from PySide6QtAds import (
#             CDockManager, CDockWidget,
#             LeftDockWidgetArea, RightDockWidgetArea,
#             TopDockWidgetArea, BottomDockWidgetArea,
#             CenterDockWidgetArea
#         )
#     except ImportError:
#         try:
#             from ads import (
#                 CDockManager, CDockWidget,
#                 LeftDockWidgetArea, RightDockWidgetArea,
#                 TopDockWidgetArea, BottomDockWidgetArea,
#                 CenterDockWidgetArea
#             )
#         except ImportError:
#             print("错误：请安装 PySide6-QtAds")
#             print("pip install PySide6-QtAds")
#             sys.exit(1)
#
#     QT_BINDING = "PySide6"
#
# elif BACKEND == "pyqt5":
#     # PyQt5 导入
#     from PyQt5.QtWidgets import (
#         QApplication, QMainWindow, QWidget, QVBoxLayout,
#         QLabel, QTextEdit, QPushButton, QMenuBar, QToolBar,
#         QStatusBar, QFileDialog, QMessageBox
#     )
#     from PyQt5.QtCore import QFile, QTextStream, Qt, QTimer
#     from PyQt5.QtWidgets import QAction  # PyQt5 中 QAction 在 QtWidgets 中
#
#     # PyQtAds 导入 (PyQt5 版本的 QtAds)
#     try:
#         from PyQtAds import (
#             CDockManager, CDockWidget,
#             LeftDockWidgetArea, RightDockWidgetArea,
#             TopDockWidgetArea, BottomDockWidgetArea,
#             CenterDockWidgetArea
#         )
#     except ImportError:
#         try:
#             from ads import (
#                 CDockManager, CDockWidget,
#                 LeftDockWidgetArea, RightDockWidgetArea,
#                 TopDockWidgetArea, BottomDockWidgetArea,
#                 CenterDockWidgetArea
#             )
#         except ImportError:
#             print("错误：请安装 PyQtAds")
#             print("pip install PyQtAds")
#             sys.exit(1)
#
#     QT_BINDING = "PyQt5"
#
# else:
#     print(f"错误：不支持的后端 '{BACKEND}'")
#     print("请使用 'pyside6' 或 'pyqt5'")
#     sys.exit(1)
#
# print(f"使用后端: {QT_BINDING}")
#
#
# class MainWindow(QMainWindow):
#     def __init__(self):
#         super().__init__()
#         self.setWindowTitle(f"QtAds 样式测试 ({QT_BINDING})")
#         self.resize(1200, 800)
#
#         # 设置中央控件（ADS 可以不需要中央控件，但保留用于兼容性）
#         central_widget = QWidget()
#         central_widget.setLayout(QVBoxLayout())
#         self.setCentralWidget(central_widget)
#
#         # 创建 Dock Manager
#         self.dock_manager = CDockManager(self)
#
#         # 创建菜单栏
#         self.create_menu()
#
#         # 创建工具栏
#         self.create_toolbar()
#
#         # 创建状态栏
#         self.setStatusBar(QStatusBar())
#         self.statusBar().showMessage(f"就绪 - 当前后端: {QT_BINDING}")
#
#         # 创建 Dock 窗口
#         self.create_dock_widgets()
#
#         # 加载样式表
#         self.load_stylesheet("resources/lightstyle.qss")
#
#     def create_menu(self):
#         menubar = self.menuBar()
#         file_menu = menubar.addMenu("文件(&F)")
#
#         exit_action = QAction("退出(&X)", self)
#         exit_action.triggered.connect(self.close)
#         file_menu.addAction(exit_action)
#
#         view_menu = menubar.addMenu("视图(&V)")
#         reload_style_action = QAction("重新加载样式(&R)", self)
#         reload_style_action.triggered.connect(lambda: self.load_stylesheet("resources/lightstyle.qss"))
#         view_menu.addAction(reload_style_action)
#
#         # 添加后端信息显示
#         help_menu = menubar.addMenu("帮助(&H)")
#         about_action = QAction(f"关于 (后端: {QT_BINDING})", self)
#         about_action.triggered.connect(self.show_about)
#         help_menu.addAction(about_action)
#
#     def create_toolbar(self):
#         toolbar = QToolBar("主工具栏")
#         self.addToolBar(toolbar)
#
#         btn_add = QPushButton("添加 Dock")
#         btn_add.clicked.connect(self.add_new_dock)
#         toolbar.addWidget(btn_add)
#
#         btn_style = QPushButton("切换样式")
#         btn_style.clicked.connect(self.toggle_style)
#         toolbar.addWidget(btn_style)
#
#         # 添加后端标识标签
#         toolbar.addSeparator()
#         backend_label = QLabel(f"[{QT_BINDING}]")
#         backend_label.setStyleSheet("color: gray; font-weight: bold;")
#         toolbar.addWidget(backend_label)
#
#     def create_dock_widgets(self):
#         """创建初始的 Dock 窗口"""
#         # Dock 1: 文件浏览器（左侧）
#         dock1 = CDockWidget("文件浏览器", self)
#         content1 = QWidget()
#         layout1 = QVBoxLayout(content1)
#         label1 = QLabel(f"🗂️ 文件列表区域\\n\\n后端: {QT_BINDING}\\n支持拖拽停靠到任意位置")
#         label1.setAlignment(Qt.AlignCenter)
#         layout1.addWidget(label1)
#         layout1.addWidget(QPushButton("测试按钮"))
#         dock1.setWidget(content1)
#         self.dock_manager.addDockWidget(LeftDockWidgetArea, dock1)
#
#         # Dock 2: 代码编辑器（右侧）
#         dock2 = CDockWidget("代码编辑器", self)
#         editor = QTextEdit()
#         editor.setPlainText(f"# 欢迎使用 QtAds ({QT_BINDING})\\n"
#                            "# 这个窗口可以拖拽、浮动、标签化\\n\\n"
#                            f"print('Hello {QT_BINDING} QtAds!')")
#         dock2.setWidget(editor)
#         self.dock_manager.addDockWidget(RightDockWidgetArea, dock2)
#
#         # Dock 3: 属性面板（右侧，与 Dock2 标签化）
#         dock3 = CDockWidget("属性", self)
#         content3 = QWidget()
#         layout3 = QVBoxLayout(content3)
#         layout3.addWidget(QLabel("⚙️ 对象属性"))
#         layout3.addWidget(QTextEdit("属性值..."))
#         dock3.setWidget(content3)
#         self.dock_manager.addDockWidgetTab(RightDockWidgetArea, dock3)
#
#         # Dock 4: 输出控制台（底部）
#         dock4 = CDockWidget("输出", self)
#         console = QTextEdit()
#         console.setPlainText(f"> 程序启动成功\\n> 后端: {QT_BINDING}\\n> QtAds 系统运行中...")
#         console.setReadOnly(True)
#         dock4.setWidget(console)
#         self.dock_manager.addDockWidget(BottomDockWidgetArea, dock4)
#
#         # Dock 5: 工具箱（顶部）
#         dock5 = CDockWidget("工具箱", self)
#         toolbox = QWidget()
#         toolbox_layout = QVBoxLayout(toolbox)
#         for i in range(5):
#             toolbox_layout.addWidget(QPushButton(f"工具 {i+1}"))
#         dock5.setWidget(toolbox)
#         self.dock_manager.addDockWidget(TopDockWidgetArea, dock5)
#         # self.dock_manager.setStyleSheet("")
#
#     def add_new_dock(self):
#         """动态添加新的 Dock 窗口"""
#         count = len(self.dock_manager.dockWidgets())
#         new_dock = CDockWidget(f"新窗口 {count + 1}", self)
#
#         content = QWidget()
#         layout = QVBoxLayout(content)
#         layout.addWidget(QLabel(f"这是动态创建的窗口 #{count + 1}\\n后端: {QT_BINDING}"))
#         layout.addWidget(QTextEdit("内容..."))
#         new_dock.setWidget(content)
#
#         # 添加到右侧区域
#         self.dock_manager.addDockWidget(RightDockWidgetArea, new_dock)
#         self.statusBar().showMessage(f"已添加新窗口: 新窗口 {count + 1}", 3000)
#
#     def load_stylesheet(self, filename):
#         """加载 QSS 样式文件"""
#         if not os.path.exists(filename):
#             print(f"警告: 找不到样式文件 {filename}，使用默认样式")
#             self.setStyleSheet("")  # 清空样式
#             return
#
#         file = QFile(filename)
#         if file.open(QFile.ReadOnly | QFile.Text):
#             stream = QTextStream(file)
#             stylesheet = stream.readAll()
#             file.close()
#
#             # 应用到整个应用程序
#             QApplication.instance().setStyleSheet(stylesheet)
#             self.statusBar().showMessage(f"已加载样式: {filename}", 3000)
#             print(f"成功加载样式文件: {filename}")
#         else:
#             print(f"错误: 无法读取文件 {filename}")
#
#     def toggle_style(self):
#         """切换样式（示例功能）"""
#         # current = QApplication.instance().styleSheet()
#         # if current:
#         #     QApplication.instance().setStyleSheet("")
#         #     self.statusBar().showMessage("已切换到系统默认样式", 3000)
#         # else:
#         #     self.load_stylesheet("resources/lightstyle.qss")
#
#         self.load_stylesheet("resources/lightstyle.qss")
#
#     def show_about(self):
#         """显示关于对话框"""
#         QMessageBox.information(
#             self,
#             "关于",
#             f"<h2>QtAds 样式测试程序</h2>"
#             f"<p>当前使用的 Qt 后端: <b>{QT_BINDING}</b></p>"
#             f"<p>支持的后端:</p>"
#             f"<ul>"
#             f"<li>PySide6 + PySide6QtAds</li>"
#             f"<li>PyQt5 + PyQtAds</li>"
#             f"</ul>"
#             f"<p>切换后端方法:</p>"
#             f"<pre>export QT_BACKEND=pyqt5  # Linux/Mac</pre>"
#             f"<pre>set QT_BACKEND=pyqt5     # Windows</pre>"
#         )
#
#
# def main():
#
#     app = QApplication(sys.argv)
#     app.setApplicationName("QtAdsTest")
#
#     window = MainWindow()
#     window.show()
#
#     # 处理 PyQt5 和 PySide6 的事件循环差异
#     if BACKEND == "pyqt5":
#         sys.exit(app.exec_())
#     else:
#         sys.exit(app.exec())
#
#
# if __name__ == "__main__":
#     main()
#
#
# print("✅ 修复后的代码已保存")
# print("\n使用说明:")
# print("=" * 50)
# print("1. 默认使用 PySide6 后端")
# print("2. 切换到 PyQt5 后端的方法:")
# print("   - Linux/Mac: export QT_BACKEND=pyqt5 && python dock_app_dual_backend.py")
# print("   - Windows: set QT_BACKEND=pyqt5 && python dock_app_dual_backend.py")
# print("   - 或直接修改代码中的 BACKEND 变量")
# print("\n3. 需要安装的依赖:")
# print("   - PySide6 模式: pip install PySide6 PySide6-QtAds")
# print("   - PyQt5 模式:   pip install PyQt5 PyQtAds")
# print("=" * 50)
#
# # import PyQtAds


# 创建支持自动切换 light/dark 主题的代码

import sys

import qdarkstyle
from PySide6.QtWidgets import QApplication, QMainWindow, QTextEdit, QLabel, QVBoxLayout, QWidget
from PySide6.QtCore import Qt
from PySide6QtAds import CDockManager, CDockWidget, DockWidgetArea


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6-QtAds 自动主题切换")
        self.resize(900, 600)

        # 创建 DockManager
        self.dock_manager = CDockManager(self)
        self.setCentralWidget(self.dock_manager)

        # 创建停靠窗口
        self.create_dock_widgets()

        # 初始应用主题
        self.apply_theme()

        # 监听系统主题切换（Qt 6.5+）
        app = QApplication.instance()
        app.styleHints().colorSchemeChanged.connect(self.apply_theme)

    def create_dock_widgets(self):
        # 1. 主编辑区 - 中心
        editor = QTextEdit()
        editor.setPlainText("代码编辑区域")
        dock_editor = CDockWidget("编辑器")
        dock_editor.setWidget(editor)
        self.dock_manager.addDockWidget(DockWidgetArea.CenterDockWidgetArea, dock_editor)

        # 2. 文件浏览器 - 左侧
        file_widget = QLabel("文件树\nproject/\n  src/\n  docs/")
        file_widget.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        dock_files = CDockWidget("项目")
        dock_files.setWidget(file_widget)
        self.dock_manager.addDockWidget(DockWidgetArea.LeftDockWidgetArea, dock_files)

        # 3. 属性面板 - 右侧
        prop_widget = QLabel("属性\n- 名称: \n- 类型: \n- 大小:")
        dock_props = CDockWidget("属性")
        dock_props.setWidget(prop_widget)
        self.dock_manager.addDockWidget(DockWidgetArea.RightDockWidgetArea, dock_props)

        # 4. 输出控制台 - 底部
        console = QTextEdit()
        console.setPlainText("> 程序启动...\n> 等待输入...")
        console.setMaximumHeight(150)
        dock_console = CDockWidget("控制台")
        dock_console.setWidget(console)
        self.dock_manager.addDockWidget(DockWidgetArea.BottomDockWidgetArea, dock_console)
        # self.dock_manager.setStyleSheet('')

    def apply_theme(self):
        app = QApplication.instance()
        if app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
            self.load_qss("resources/darkstyle.qss")
        else:
            self.load_qss("resources/lightstyle.qss")

    def load_qss(self, filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                QApplication.instance().setStyleSheet(f.read())
                self.dock_manager.setStyleSheet('')
        except FileNotFoundError:
            # 文件不存在时使用内置默认样式
            self.set_fallback_style()

    def set_fallback_style(self):
        app = QApplication.instance()
        is_dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark

        if is_dark:
            self.load_qss("resources/darkstyle.qss")
        else:
            self.load_qss("resources/lightstyle.qss")

        # if is_dark:
        #     app.setStyleSheet("""
        #         QMainWindow { background-color: #2b2b2b; }
        #         QTextEdit { background-color: #1e1e1e; color: #d4d4d4; border: 1px solid #3c3c3c; }
        #         QLabel { color: #cccccc; padding: 5px; }
        #         ads--CDockWidgetTab { background-color: #2d2d30; color: #cccccc; padding: 4px 10px; }
        #         ads--CDockWidgetTab[activeTab="true"] { background-color: #1e1e1e; color: #ffffff; border-bottom: 2px solid #007acc; }
        #     """)
        # else:
        #     app.setStyleSheet("""
        #         QMainWindow { background-color: #f3f3f3; }
        #         QTextEdit { background-color: #ffffff; color: #333333; border: 1px solid #d4d4d4; }
        #         QLabel { color: #333333; padding: 5px; }
        #         ads--CDockWidgetTab { background-color: #ececec; color: #333333; padding: 4px 10px; }
        #         ads--CDockWidgetTab[activeTab="true"] { background-color: #ffffff; color: #000000; border-bottom: 2px solid #007acc; }
        #     """)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # 使用Fusion风格确保一致性

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
