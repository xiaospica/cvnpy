# 需求目标
你当前做了两份“整包复制”，目标是让它们能与原包 **同时安装/同时加载** 而不发生命名与事件冲突：

1. `vnpy_signal_strategy_plus`：包内相关类统一加 `Plus` 后缀（如 `SignalEnginePlus`），并修复复制后仍引用原包 `vnpy_signal_strategy` 的 import，做到与原版彻底隔离。
2. `vnpy_signal_strategy_plus_backtester`：将对外呈现的 `CtaBacktester` 改为 `SignalBacktester`，并评估需要改名的其他类/常量/资源，使其不会与 `vnpy_ctabacktester` 冲突。

---

# 现状分析（只读扫描结果摘要）

## A. vnpy_signal_strategy_plus
该包目前是 `vnpy_signal_strategy` 的拷贝，但存在三个关键问题：

1. **import 串包**：`vnpy_signal_strategy_plus/__init__.py`、`engine.py`、`mysql_signal_strategy.py`、`strategies/multistrategy_signal_strategy.py` 等仍然 `from vnpy_signal_strategy ...`，导致运行时实际用到原包类，plus 包形同“壳”。
2. **对外注册名冲突**：
   - `APP_NAME = "SignalStrategy"` 与原版冲突
   - `SignalEngine.engine_name = "SignalStrategy"` 与原版冲突
   - UI 里事件字符串 `"EVENT_SIGNAL_STRATEGY"` 与原版冲突
   - locale gettext domain 仍是 `vnpy_signal_strategy`，会复用/覆盖原包翻译域
3. **对外 App 类名不匹配**：工程侧 `run_sim.py` 目前导入 `SignalStrategyPlusApp`，但 plus 包实际定义的是 `SignalStrategyApp`。

结论：如果希望原版与 plus 版同时存在，必须同时改：
“类名 + APP_NAME/engine_name + 事件字符串 + locale domain + import 全面指向本包”。

## B. vnpy_signal_strategy_plus_backtester
该包目前是 `vnpy_ctabacktester` 的拷贝，关键冲突点集中在：

1. **APP_NAME 仍是 `"CtaBacktester"`**（引擎注册/获取都靠它，会与官方 backtester 引擎同名冲突）
2. **App 类名仍是 `CtaBacktesterApp`**，display 名称仍是 “CTA回测”
3. **事件字符串仍是 `eBacktesterLog/eBacktesterBacktestingFinished/eBacktesterOptimizationFinished`**（事件总线字符串层面会碰撞）
4. **设置文件名仍为 `cta_backtester_setting.json`**（会和官方回测配置写到同一路径发生覆盖）
5. **i18n 仍使用 `vnpy_ctabacktester` 域与对应文件名/build_hook 脚本**（会复用官方域，导致翻译与构建产物冲突）

结论：至少需要把“APP_NAME + App 类名 + Engine 类名 + widget_name + 事件字符串 + 设置文件名 + locale domain/文件/脚本”改为全新命名空间。

---

# 命名与改造策略（确定性规则）

## 1）vnpy_signal_strategy_plus：统一加 Plus 后缀（全量）
原则：**plus 包内自定义类全部加 `Plus` 后缀**，并保证跨文件引用一致。

建议映射（核心对外 + 关键内部）：
- `SignalStrategyApp` → `SignalStrategyPlusApp`
- `SignalEngine` → `SignalEnginePlus`
- `SignalTemplate` → `SignalTemplatePlus`
- `MySQLSignalStrategy` → `MySQLSignalStrategyPlus`
- `AutoResubmitMixin` → `AutoResubmitMixinPlus`
- `Stock` → `StockPlus`（SQLAlchemy Model，避免调试/反射时混淆）
- `MultiStrategySignalStrategy` → `MultiStrategySignalStrategyPlus`
- UI：`SignalStrategyWidget/SignalStrategyManager/DataMonitor/LogMonitor/...` → 全部加 `Plus`

同时将对外注册信息改为唯一值：
- `APP_NAME`: `"SignalStrategy"` → `"SignalStrategyPlus"`
- `engine_name`: `"SignalStrategy"` → `"SignalStrategyPlus"`
- 事件字符串：`"EVENT_SIGNAL_STRATEGY"` → `"EVENT_SIGNAL_STRATEGY_PLUS"`
- gettext domain：`vnpy_signal_strategy` → `vnpy_signal_strategy_plus`

## 2）vnpy_signal_strategy_plus_backtester：统一改为 SignalBacktester 命名空间（全量）
原则：该包希望与 `vnpy_ctabacktester` 并存时不冲突，因此**包内类名与全局字符串常量统一改为 SignalBacktester 前缀**（或后缀），这里采用前缀以可读性更强。

建议映射（对外暴露与全局字符串必改）：
- `APP_NAME`: `"CtaBacktester"` → `"SignalBacktester"`
- `CtaBacktesterApp` → `SignalBacktesterApp`
- `BacktesterEngine` → `SignalBacktesterEngine`
- `widget_name`: `"BacktesterManager"` → `"SignalBacktesterManager"`
- `BacktesterManager` → `SignalBacktesterManager`
- 事件常量：
  - `EVENT_BACKTESTER_LOG`: `"eBacktesterLog"` → `"eSignalBacktesterLog"`
  - `EVENT_BACKTESTER_BACKTESTING_FINISHED`: `"eBacktesterBacktestingFinished"` → `"eSignalBacktesterBacktestingFinished"`
  - `EVENT_BACKTESTER_OPTIMIZATION_FINISHED`: `"eBacktesterOptimizationFinished"` → `"eSignalBacktesterOptimizationFinished"`
- 设置文件名：`cta_backtester_setting.json` → `signal_backtester_setting.json`
- gettext domain：`vnpy_ctabacktester` → `vnpy_signal_backtester`（或与包名一致的唯一域）

为彻底“区分/可搜索”，UI 文件内其余类也统一改名前缀（推荐执行）：
- `BacktesterChart/OptimizationSettingEditor/...` → `SignalBacktesterChart/SignalOptimizationSettingEditor/...`
- `BacktestingTradeMonitor/...` → `SignalBacktestingTradeMonitor/...`
- `BacktestingResultDialog/CandleChartDialog/...` → `SignalBacktestingResultDialog/SignalCandleChartDialog/...`

---

# 具体实施步骤（后续执行将严格按此列表推进）

## Step 0：建立改名清单与影响面（只读确认）
1. 全工程检索 `vnpy_signal_strategy_plus` 中：
   - `from vnpy_signal_strategy`、`APP_NAME = "SignalStrategy"`、`engine_name = "SignalStrategy"`、`EVENT_SIGNAL_STRATEGY`
2. 全工程检索 `vnpy_signal_strategy_plus_backtester` 中：
   - `CtaBacktester`、`APP_NAME = "CtaBacktester"`、`eBacktester`、`cta_backtester_setting.json`、`vnpy_ctabacktester`
3. 记录需要联动修改的包外入口（如 `run_sim.py`）与可能的资源文件（ico/locale）。

## Step 1：改造 vnpy_signal_strategy_plus（类名 + 事件/APP_NAME + import）
1. `vnpy_signal_strategy_plus/__init__.py`
   - 将所有 `from vnpy_signal_strategy...` 改为本包相对 import
   - `SignalStrategyApp` 改为 `SignalStrategyPlusApp`
   - `app_name` 改为 `"SignalStrategyPlus"`
   - `engine_class/widget_name` 指向 plus 版类名
   - 更新 `__all__`（以及必要时对外导出 App/核心类）
2. `engine.py`
   - `SignalEngine` → `SignalEnginePlus`
   - `APP_NAME/engine_name` 改为 `"SignalStrategyPlus"`
   - `"EVENT_SIGNAL_STRATEGY"` 改为 `"EVENT_SIGNAL_STRATEGY_PLUS"`
   - import 改为 `from .template import SignalTemplatePlus`、`from .mysql_signal_strategy import MySQLSignalStrategyPlus`
   - 动态加载策略时的基类判断同步更新为 plus 版类名
3. `template.py`
   - `SignalTemplate` → `SignalTemplatePlus`
   - `TYPE_CHECKING` 与类型注解引用引擎类同步为 `SignalEnginePlus`
4. `auto_resubmit.py`
   - `AutoResubmitMixin` → `AutoResubmitMixinPlus`
5. `mysql_signal_strategy.py`
   - `MySQLSignalStrategy` → `MySQLSignalStrategyPlus`
   - `Stock` → `StockPlus`
   - import 改为相对引用 plus 版模板与 mixin
6. `strategies/multistrategy_signal_strategy.py`
   - `MultiStrategySignalStrategy` → `MultiStrategySignalStrategyPlus`
   - import 改为相对引用 `..mysql_signal_strategy`
7. `ui/__init__.py`、`ui/widget.py`
   - UI 内所有自定义类统一加 `Plus`
   - `main_engine.get_engine(APP_NAME)` 取 plus 版 `APP_NAME`
   - 事件注册改为 `"EVENT_SIGNAL_STRATEGY_PLUS"`
8. `locale/__init__.py`
   - gettext domain 改为 `vnpy_signal_strategy_plus`
   - 若当前无 `.mo` 文件，则确保 fallback 行为正确（不影响运行）

## Step 2：改造 vnpy_signal_strategy_plus_backtester（SignalBacktester 命名空间）
1. `engine.py`
   - `APP_NAME` 改为 `"SignalBacktester"`
   - `BacktesterEngine` → `SignalBacktesterEngine`
   - `EVENT_*` 三个字符串全部改为 `eSignalBacktester...`
   - 引擎写日志、回测完成、优化完成事件推送的常量引用同步更新
2. `__init__.py`
   - `CtaBacktesterApp` → `SignalBacktesterApp`
   - `engine_class` 指向 `SignalBacktesterEngine`
   - `widget_name` 改为 `"SignalBacktesterManager"`
   - `display_name` 从 “CTA回测” 改为更符合定位的名称（如 “Signal回测/信号回测”）
   - 修正 `all` 为 `__all__`（避免导出不生效，属于低风险纠错）
3. `ui/widget.py`
   - `BacktesterManager` → `SignalBacktesterManager`
   - `setting_filename` 改为 `signal_backtester_setting.json`
   - 文件内其余类统一加 `Signal...` 前缀（按本方案执行“全量区分”）
   - 所有引用到 `BacktesterManager`、事件常量、窗口标题字符串同步更新
4. `ui/__init__.py`
   - 若有从 widget 导入类名，按新类名同步修改
5. `locale` 目录（domain 与构建脚本必须同步）
   - `locale/__init__.py` 的 domain 改为 `vnpy_signal_backtester`
   - `vnpy_ctabacktester.pot/.po/.mo` 文件名改为 `vnpy_signal_backtester.*`
   - `locale/build_hook.py` 内硬编码路径/文件名全部替换为新域 + 本包路径
   - `locale/generate_pot.bat` 同步修改（若你仍需要生成 pot）

## Step 3：包外联动修复（保证示例入口可用）
1. `run_sim.py`（或你实际启动脚本）
   - 若要加载 plus 版信号策略：导入 `SignalStrategyPlusApp`、plus 版策略类，并使用 `get_engine("SignalStrategyPlus")`
   - 若要加载 SignalBacktester：导入 `SignalBacktesterApp`（视主程序加载方式而定）

---

# 验证与回归检查（执行阶段会做）
1. 纯导入检查：
   - `python -c "import vnpy_signal_strategy_plus as m; print(m.SignalStrategyPlusApp.app_name)"`
   - `python -c "import vnpy_signal_strategy_plus_backtester as m; print(m.SignalBacktesterApp.app_name)"`
2. 同时安装/同时加载场景：
   - 同时 import 原包与 plus 包，确保 `APP_NAME/engine_name/event` 不冲突
3. GUI 启动冒烟：
   - 打开对应 App 的界面，确认能初始化引擎、能正常写日志、事件回调能触发

---

# 风险点与规避
- 反射/序列化依赖类名：如策略配置里存 `class_name`，改名后需要同步更新配置文件中的类名字段。
- 事件名改动：任何外部模块若硬编码监听旧事件字符串，需要一起更新。
- locale 构建脚本：若你不发布 wheel，可只保证运行时 fallback；若要发布，必须同步改 build_hook 与 po/mo 文件名。

